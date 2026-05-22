# -*- coding: utf-8 -*-
"""
NxtWave Invoice Generation Service
-----------------------------------
POST /generate-invoice
  → copies Google Docs master template
  → fills FF* placeholders
  → exports to PDF
  → uploads to Google Drive
  → PATCHes Salesforce record with:
      Invoice_Links__c
      Invoice_status__c
      Follow_Up_Date_Time_NBFC__c
"""

import io
import os
import re
import logging
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from google.oauth2 import service_account
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from num2words import num2words
from pydantic import BaseModel

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# CONFIG  (from environment variables)
# ──────────────────────────────────────────────
API_KEY              = os.environ["X_API_KEY"]
TEMPLATE_FILE_ID     = os.environ["GOOGLE_TEMPLATE_FILE_ID"]
DOCS_FOLDER_ID       = os.environ["GOOGLE_DOCS_FOLDER_ID"]
PDFS_FOLDER_ID       = os.environ["GOOGLE_PDFS_FOLDER_ID"]
SF_LOGIN_DOMAIN      = os.environ["SF_LOGIN_DOMAIN"]
SF_API_VERSION       = os.environ.get("SF_API_VERSION", "61.0")
SF_CLIENT_ID         = os.environ["SF_CLIENT_ID"]
SF_CLIENT_SECRET     = os.environ["SF_CLIENT_SECRET"]

# Optional: path to service account JSON (for local dev)
# On Cloud Run, uses the default service account automatically
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

# ──────────────────────────────────────────────
# PLACEHOLDER MAP  (template FF* token → payload field)
# ──────────────────────────────────────────────
PLACEHOLDER_MAP = {
    "FFInvoiceNumber":     "invoiceNumber",
    "FFBillToName":        "billTo",
    "FFShipToName":        "shipTo",
    "FFState":             "state",
    "FFLoanApplicantName": "loanApplicantName",
    "FFPlaceOfSupply":     "placeOfSupply",
    "FFInvoiceDate":       "invoiceDate",
    "FFItemName":          "itemName",
    "FFRate":              "rate",
    "FFTaxableAmount":     "taxableAmount",
    "FFLoanAmount":        "loanAmount",
    "FFTotalInWords":      "_amountInWords",   # computed from rate
    "FFLoanTenure":        "loanTenure",
    "FFUID":               "uid",
    "FFNBFCName":          "nbfcName",
}

# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def inr_words(amount_str: str) -> str:
    try:
        clean = str(amount_str).replace(",", "").strip()
        rupees = int(round(float(clean)))
        return num2words(rupees, lang="en_IN").replace("-", " ").title() + " Rupees Only"
    except Exception:
        return ""

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:200]

# ──────────────────────────────────────────────
# GOOGLE CLIENTS  (lazy singleton)
# ──────────────────────────────────────────────
_drive_client = None
_docs_client  = None
_google_creds_expiry: float = 0

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

def get_google_clients():
    global _drive_client, _docs_client, _google_creds_expiry
    if _drive_client is None or time.time() > _google_creds_expiry:
        if SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
            # Local dev: use service account JSON file
            creds = service_account.Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
        else:
            # Cloud Run: use attached service account automatically
            creds, _ = google_auth_default(scopes=SCOPES)
        _drive_client = build("drive", "v3", credentials=creds)
        _docs_client  = build("docs",  "v1", credentials=creds)
        _google_creds_expiry = time.time() + 3000
    return _drive_client, _docs_client

# ──────────────────────────────────────────────
# SALESFORCE CLIENT
# ──────────────────────────────────────────────
_sf_token:    str | None = None
_sf_instance: str | None = None
_sf_token_expiry: float = 0

def get_sf_token() -> tuple[str, str]:
    global _sf_token_expiry
    for url in [
        f"{SF_LOGIN_DOMAIN}/services/oauth2/token",
        "https://login.salesforce.com/services/oauth2/token",
    ]:
        try:
            r = requests.post(url, data={
                "grant_type":    "client_credentials",
                "client_id":     SF_CLIENT_ID,
                "client_secret": SF_CLIENT_SECRET,
            }, timeout=30)
            if r.status_code == 200:
                j = r.json()
                _sf_token_expiry = time.time() + 6900
                return j["access_token"], j.get("instance_url", SF_LOGIN_DOMAIN)
        except Exception as e:
            log.warning("SF token attempt failed: %s", e)
    raise RuntimeError("Could not obtain Salesforce access token.")

def sf_patch(record_id: str, payload: dict):
    global _sf_token, _sf_instance
    if not _sf_token or time.time() > _sf_token_expiry:
        _sf_token, _sf_instance = get_sf_token()

    url = f"{_sf_instance}/services/data/v{SF_API_VERSION}/sobjects/NBFC_Onboarding__c/{record_id}"
    headers = {
        "Authorization": f"Bearer {_sf_token}",
        "Content-Type":  "application/json",
    }
    r = requests.patch(url, json=payload, headers=headers, timeout=30)

    # Token expired → refresh once and retry
    if r.status_code == 401:
        _sf_token, _sf_instance = get_sf_token()
        headers["Authorization"] = f"Bearer {_sf_token}"
        r = requests.patch(url, json=payload, headers=headers, timeout=30)

    if r.status_code not in (200, 204):
        raise RuntimeError(f"SF PATCH failed {r.status_code}: {r.text[:500]}")

# ──────────────────────────────────────────────
# PYDANTIC REQUEST MODEL
# ──────────────────────────────────────────────
class InvoicePayload(BaseModel):
    uid:               str
    nbfcName:          str
    loanTenure:        str
    taxableAmount:     str
    loanAmount:        str
    invoiceNumber:     str
    billTo:            str
    shipTo:            str
    state:             str
    loanApplicantName: str
    placeOfSupply:     str
    invoiceDate:       str
    itemName:          str
    rate:              str
    recordId:          str

# ──────────────────────────────────────────────
# FASTAPI APP
# ──────────────────────────────────────────────
app = FastAPI(
    title="NxtWave Invoice Generation Service",
    version="1.0.0",
)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/generate-invoice")
def generate_invoice(
    payload: InvoicePayload,
    x_api_key: str = Header(..., alias="x-api-key"),
):
    # ── Auth ──────────────────────────────────
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    log.info("Invoice request | uid=%s nbfc=%s record=%s",
             payload.uid, payload.nbfcName, payload.recordId)

    drive, docs = get_google_clients()

    # ── Build data dict ───────────────────────
    data = payload.model_dump()
    data["_amountInWords"] = inr_words(payload.rate)

    # ── Step 1: Copy master template ──────────
    doc_name = sanitize_filename(
        f"{payload.invoiceNumber}_{payload.billTo}_{payload.uid}"
    )
    try:
        copied = drive.files().copy(
            fileId=TEMPLATE_FILE_ID,
            body={"name": doc_name, "parents": [DOCS_FOLDER_ID]},
            fields="id",
        ).execute()
    except Exception as e:
        log.error("Template copy failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Template copy failed: {e}")

    doc_id  = copied["id"]
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    log.info("Doc created: %s", doc_id)

    # ── Step 2: Replace FF* placeholders ──────
    reqs = []
    for token, field in PLACEHOLDER_MAP.items():
        val = str(data.get(field) or "")
        reqs.append({
            "replaceAllText": {
                "containsText": {"text": token, "matchCase": True},
                "replaceText":  val,
            }
        })
    try:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": reqs},
        ).execute()
    except Exception as e:
        log.error("Placeholder fill failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Placeholder fill failed: {e}")

    # ── Step 3: Export Doc → PDF ───────────────
    try:
        pdf_bytes = drive.files().export(
            fileId=doc_id, mimeType="application/pdf"
        ).execute()
    except Exception as e:
        log.error("PDF export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"PDF export failed: {e}")

    # ── Step 4: Upload PDF to Drive ────────────
    pdf_name = sanitize_filename(
        f"{payload.invoiceNumber}_{payload.billTo}_{payload.uid}"
    ) + ".pdf"
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False
    )
    try:
        created = drive.files().create(
            body={"name": pdf_name, "parents": [PDFS_FOLDER_ID]},
            media_body=media,
            fields="id",
        ).execute()
        pdf_file_id = created["id"]
        drive.permissions().create(
            fileId=pdf_file_id,
            body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()
    except Exception as e:
        log.error("PDF upload failed: %s", e)
        raise HTTPException(status_code=500, detail=f"PDF upload failed: {e}")

    pdf_url = f"https://drive.google.com/file/d/{pdf_file_id}/view?usp=sharing"
    log.info("PDF ready: %s", pdf_url)

    # ── Step 5: PATCH Salesforce record ────────
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    try:
        sf_patch(payload.recordId, {
            "Invoice_Links__c":            pdf_url,
            "Invoice_status__c":           "Invoice Generated",
            "Follow_Up_Date_Time_NBFC__c": now_utc,
        })
        log.info("SF record %s updated.", payload.recordId)
        log.info(
            "AUDIT | record_id=%s invoice=%s pdf=%s uid=%s nbfc=%s timestamp=%s",
            payload.recordId,
            payload.invoiceNumber,
            pdf_url,
            payload.uid,
            payload.nbfcName,
            now_utc
        )
    except Exception as e:
        log.error("Salesforce PATCH failed: %s", e)
        # Invoice already generated — return partial success with PDF URL
        return JSONResponse(status_code=207, content={
            "status":   "partial_success",
            "message":  "Invoice generated but Salesforce update failed.",
            "pdf_url":  pdf_url,
            "doc_url":  doc_url,
            "sf_error": str(e),
        })

    return {
        "status":       "success",
        "message":      "Invoice generated and Salesforce record updated.",
        "pdf_url":      pdf_url,
        "doc_url":      doc_url,
        "record_id":    payload.recordId,
        "generated_at": now_utc,
    }
