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
from google.oauth2.credentials import Credentials as UserCredentials
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from num2words import num2words
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_KEY              = os.environ["X_API_KEY"]
TEMPLATE_FILE_ID     = os.environ["GOOGLE_TEMPLATE_FILE_ID"]
DOCS_FOLDER_ID       = os.environ["GOOGLE_DOCS_FOLDER_ID"]
PDFS_FOLDER_ID       = os.environ["GOOGLE_PDFS_FOLDER_ID"]
SF_LOGIN_DOMAIN      = os.environ["SF_LOGIN_DOMAIN"]
SF_API_VERSION       = os.environ.get("SF_API_VERSION", "61.0")
SF_CLIENT_ID         = os.environ["SF_CLIENT_ID"]
SF_CLIENT_SECRET     = os.environ["SF_CLIENT_SECRET"]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

GOOGLE_OAUTH_CLIENT_ID     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
ENABLE_DEBUG_ENDPOINTS     = os.environ.get("ENABLE_DEBUG_ENDPOINTS", "false").lower() == "true"

def parse_int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        log.warning("Invalid integer for %s. Using default: %s", name, default)
        return default

INVOICE_WEBHOOK_URL = os.environ.get("INVOICE_WEBHOOK_URL", "")
INVOICE_WEBHOOK_TIMEOUT_SECONDS = parse_int_env("INVOICE_WEBHOOK_TIMEOUT_SECONDS", 30)
INVOICE_WEBHOOK_SECRET = os.environ.get("INVOICE_WEBHOOK_SECRET", "")

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
    "FFTotalInWords":      "_amountInWords",
    "FFLoanTenure":        "loanTenure",
    "FFUID":               "uid",
    "FFNBFCName":          "nbfcName",
}

def inr_words(amount_str: str) -> str:
    try:
        clean = str(amount_str).replace(",", "").strip()
        rupees = int(round(float(clean)))
        return num2words(rupees, lang="en_IN").replace("-", " ").title() + " Rupees Only"
    except Exception:
        return ""

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:200]

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
        if GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN:
            auth_mode = "oauth_user"
            creds = UserCredentials(
                token=None,
                refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=GOOGLE_OAUTH_CLIENT_ID,
                client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
                scopes=SCOPES,
            )
        elif SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
            auth_mode = "service_account_json"
            creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        else:
            auth_mode = "adc"
            creds, _ = google_auth_default(scopes=SCOPES)

        log.info("Google Auth Mode: %s", auth_mode)
        _drive_client = build("drive", "v3", credentials=creds)
        _docs_client  = build("docs",  "v1", credentials=creds)
        _google_creds_expiry = time.time() + 3000
    return _drive_client, _docs_client

_sf_token:    str | None = None
_sf_instance: str | None = None
_sf_token_expiry: float = 0

def get_sf_token() -> tuple[str, str]:
    global _sf_token_expiry
    for url in [f"{SF_LOGIN_DOMAIN}/services/oauth2/token", "https://login.salesforce.com/services/oauth2/token"]:
        try:
            r = requests.post(url, data={"grant_type": "client_credentials", "client_id": SF_CLIENT_ID, "client_secret": SF_CLIENT_SECRET}, timeout=30)
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
    headers = {"Authorization": f"Bearer {_sf_token}", "Content-Type": "application/json"}
    r = requests.patch(url, json=payload, headers=headers, timeout=30)
    if r.status_code == 401:
        _sf_token, _sf_instance = get_sf_token()
        headers["Authorization"] = f"Bearer {_sf_token}"
        r = requests.patch(url, json=payload, headers=headers, timeout=30)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"SF PATCH failed {r.status_code}: {r.text[:500]}")

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

def send_invoice_webhook(payload: InvoicePayload, pdf_url: str, generated_at: str) -> dict:
    if not INVOICE_WEBHOOK_URL:
        return {
            "sent": False,
            "skipped": True,
            "reason": "INVOICE_WEBHOOK_URL is not configured"
        }

    webhook_payload = {
        "event": "invoice.pdf_generated",
        "recordId": payload.recordId,
        "invoiceLink": pdf_url,
        "invoiceNumber": payload.invoiceNumber,
        "uid": payload.uid,
        "nbfcName": payload.nbfcName,
        "generatedAt": generated_at,
        "idempotencyKey": f"{payload.recordId}:{payload.invoiceNumber}",
    }

    headers = {"Content-Type": "application/json"}
    if INVOICE_WEBHOOK_SECRET:
        headers["x-webhook-secret"] = INVOICE_WEBHOOK_SECRET

    response = requests.post(
        INVOICE_WEBHOOK_URL,
        json=webhook_payload,
        headers=headers,
        timeout=INVOICE_WEBHOOK_TIMEOUT_SECONDS,
    )
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"Invoice webhook failed {response.status_code}: {response.text[:500]}")
    return {
        "sent": True,
        "status_code": response.status_code,
        "response": response.text[:500],
    }

app = FastAPI(title="NxtWave Invoice Generation Service", version="1.0.0")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/debug-google-auth")
def debug_google_auth(x_api_key: str = Header(..., alias="x-api-key")):
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(status_code=403, detail="Debug endpoints are disabled.")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    auth_mode = "adc"
    if GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN:
        auth_mode = "oauth_user"
    elif SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
        auth_mode = "service_account_json"

    return {
        "google_auth_mode": auth_mode,
        "has_oauth_client_id": bool(GOOGLE_OAUTH_CLIENT_ID),
        "has_oauth_client_secret": bool(GOOGLE_OAUTH_CLIENT_SECRET),
        "has_oauth_refresh_token": bool(GOOGLE_OAUTH_REFRESH_TOKEN)
    }

@app.post("/debug-drive-copy")
def debug_drive_copy(x_api_key: str = Header(..., alias="x-api-key")):
    if not ENABLE_DEBUG_ENDPOINTS:
        raise HTTPException(status_code=403, detail="Debug endpoints are disabled.")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")

    drive, _ = get_google_clients()
    copy_name = f"debug-drive-copy-{int(time.time())}"
    copy_id = None
    try:
        copied = drive.files().copy(
            fileId=TEMPLATE_FILE_ID,
            body={"name": copy_name, "parents": [DOCS_FOLDER_ID]},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        copy_id = copied["id"]

        try:
            drive.files().delete(fileId=copy_id, supportsAllDrives=True).execute()
            return {"status": "success", "message": f"Copy succeeded: {copy_id} ({copy_name}) and was deleted."}
        except Exception as delete_err:
            log.warning("Debug copy cleanup failed for %s: %s", copy_id, delete_err)
            return {
                "status": "partial_success",
                "message": "Copy succeeded but delete failed.",
                "copy_id": copy_id,
                "copied": True,
                "deleted": False,
                "delete_error": str(delete_err)
            }
    except Exception as e:
        log.error("Debug Drive copy failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Debug Drive copy failed: {str(e)}")

@app.post("/generate-invoice")
def generate_invoice(payload: InvoicePayload, x_api_key: str = Header(..., alias="x-api-key")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key.")
    log.info("Invoice request | uid=%s nbfc=%s record=%s", payload.uid, payload.nbfcName, payload.recordId)
    drive, docs = get_google_clients()
    data = payload.model_dump()
    data["_amountInWords"] = inr_words(payload.rate)
    doc_name = sanitize_filename(f"{payload.invoiceNumber}_{payload.billTo}_{payload.uid}")
    try:
        copied = drive.files().copy(fileId=TEMPLATE_FILE_ID, body={"name": doc_name, "parents": [DOCS_FOLDER_ID]}, fields="id", supportsAllDrives=True,).execute()
    except Exception as e:
        log.error("Template copy failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Template copy failed: {e}")
    doc_id  = copied["id"]
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    log.info("Doc created: %s", doc_id)
    reqs = []
    for token, field in PLACEHOLDER_MAP.items():
        val = str(data.get(field) or "")
        reqs.append({"replaceAllText": {"containsText": {"text": token, "matchCase": True}, "replaceText": val}})
    try:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
    except Exception as e:
        log.error("Placeholder fill failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Placeholder fill failed: {e}")
    try:
        pdf_bytes = drive.files().export(fileId=doc_id, mimeType="application/pdf").execute()
    except Exception as e:
        log.error("PDF export failed: %s", e)
        raise HTTPException(status_code=500, detail=f"PDF export failed: {e}")
    pdf_name = sanitize_filename(f"{payload.invoiceNumber}_{payload.billTo}_{payload.uid}") + ".pdf"
    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    try:
        created = drive.files().create(body={"name": pdf_name, "parents": [PDFS_FOLDER_ID]}, media_body=media, fields="id", supportsAllDrives=True,).execute()
        pdf_file_id = created["id"]
        drive.permissions().create(fileId=pdf_file_id, body={"type": "anyone", "role": "reader"}, fields="id", supportsAllDrives=True,).execute()
    except Exception as e:
        log.error("PDF upload failed: %s", e)
        raise HTTPException(status_code=500, detail=f"PDF upload failed: {e}")

    doc_deleted = False
    try:
        drive.files().delete(fileId=doc_id, supportsAllDrives=True).execute()
        log.info("Temporary doc %s deleted.", doc_id)
        doc_deleted = True
    except Exception as e:
        log.warning("Failed to delete temporary doc %s: %s", doc_id, e)

    pdf_url = f"https://drive.google.com/file/d/{pdf_file_id}/view?usp=sharing"
    log.info("PDF ready: %s", pdf_url)
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    try:
        webhook_result = send_invoice_webhook(payload, pdf_url, now_utc)
        log.info("Invoice webhook sent for record %s", payload.recordId)
    except Exception as e:
        log.error("Invoice webhook failed: %s", e)
        webhook_result = {
            "sent": False,
            "error": str(e),
        }

    try:
        sf_patch(payload.recordId, {"Invoice_Links__c": pdf_url, "Invoice_status__c": "Invoice Generated", "Follow_Up_Date_Time_NBFC__c": now_utc})
        log.info("SF record %s updated.", payload.recordId)
        log.info("AUDIT | record_id=%s invoice=%s pdf=%s uid=%s nbfc=%s timestamp=%s", payload.recordId, payload.invoiceNumber, pdf_url, payload.uid, payload.nbfcName, now_utc)
    except Exception as e:
        log.error("Salesforce PATCH failed: %s", e)
        return JSONResponse(status_code=207, content={
            "status": "partial_success",
            "message": "Invoice generated but Salesforce update failed.",
            "pdf_url": pdf_url,
            "doc_url": None,
            "temporary_doc_deleted": doc_deleted,
            "sf_error": str(e),
            "webhook_result": webhook_result
        })
    return {
        "status": "success",
        "message": "Invoice generated and Salesforce record updated.",
        "pdf_url": pdf_url,
        "doc_url": None,
        "temporary_doc_deleted": doc_deleted,
        "record_id": payload.recordId,
        "generated_at": now_utc,
        "webhook_result": webhook_result
    }
