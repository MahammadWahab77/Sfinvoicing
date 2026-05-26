# NxtWave SF Invoicing Service

### What it does
This service receives a POST payload, copies a Google Docs master template, fills placeholders with the provided data, exports the document to PDF, uploads it to Google Drive, and finally updates the corresponding Salesforce record with the invoice link and status.

### Architecture diagram
POST /generate-invoice → Auth Check → Copy Template → Fill Placeholders → Export PDF → Upload to Drive → Delete Temporary Doc → PATCH Salesforce → Return response

### Endpoint
**POST** `/generate-invoice`

**Headers:**
- `x-api-key`: Your API Key

**Request Payload:**
```json
{
  "uid": "string",
  "nbfcName": "string",
  "loanTenure": "string",
  "taxableAmount": "string",
  "loanAmount": "string",
  "invoiceNumber": "string",
  "billTo": "string",
  "shipTo": "string",
  "state": "string",
  "loanApplicantName": "string",
  "placeOfSupply": "string",
  "invoiceDate": "string",
  "itemName": "string",
  "rate": "string",
  "recordId": "string"
}
```

**Success Response (200):**
```json
{
  "status": "success",
  "message": "Invoice generated and Salesforce record updated.",
  "pdf_url": "https://drive.google.com/file/d/.../view?usp=sharing",
  "doc_url": null,
  "temporary_doc_deleted": true,
  "record_id": "...",
  "generated_at": "2023-10-27T10:00:00+00:00"
}
```

**Partial Success Response (207):**
```json
{
  "status": "partial_success",
  "message": "Invoice generated but Salesforce update failed.",
  "pdf_url": "...",
  "doc_url": null,
  "temporary_doc_deleted": true,
  "sf_error": "..."
}
```

**Error Response (401):**
```json
{
  "detail": "Invalid API key."
}
```

**Error Response (500):**
```json
{
  "detail": "Template copy failed: ..."
}
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `X_API_KEY` | Yes | API key for authenticating requests to this service. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | No | Path to Google Service Account JSON file (for local dev). |
| `GOOGLE_OAUTH_CLIENT_ID` | No | Google OAuth Client ID. Overrides service account if present. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | No | Google OAuth Client Secret. Overrides service account if present. |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | No | Google OAuth Refresh Token. Overrides service account if present. |
| `ENABLE_DEBUG_ENDPOINTS` | No | If "true", enables /debug-google-auth and /debug-drive-copy. |
| `GOOGLE_TEMPLATE_FILE_ID` | Yes | File ID of the Google Docs template. |
| `GOOGLE_DOCS_FOLDER_ID` | Yes | Folder ID where temporary Google Docs will be created. |
| `GOOGLE_PDFS_FOLDER_ID` | Yes | Folder ID where generated PDFs will be stored. |
| `SF_LOGIN_DOMAIN` | Yes | Salesforce login domain (e.g., https://login.salesforce.com). |
| `SF_API_VERSION` | No | Salesforce API version (defaults to 61.0). |
| `SF_CLIENT_ID` | Yes | Salesforce Connected App Client ID. |
| `SF_CLIENT_SECRET` | Yes | Salesforce Connected App Client Secret. |
| `INVOICE_WEBHOOK_URL` | No | Salesforce webhook endpoint URL. |
| `INVOICE_WEBHOOK_TIMEOUT_SECONDS` | No | Timeout for the webhook request (defaults to 30). |

### Note on OAuth and Debug Endpoints
- **OAuth Overrides**: If `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and `GOOGLE_OAUTH_REFRESH_TOKEN` are provided, the service uses OAuth user credentials, which avoids the `storageQuotaExceeded` error common with service accounts.
- **Debug Endpoints**: `/debug-google-auth` and `/debug-drive-copy` are only active if `ENABLE_DEBUG_ENDPOINTS` is set to `true`. These should only be enabled for troubleshooting.
- **Cleanup**: The service automatically deletes the temporary Google Docs copy after the PDF is successfully exported and uploaded to prevent Drive clutter.

### GCP Setup steps
1. Install gcloud CLI.
2. Create secrets in Secret Manager:
   ```bash
   echo -n "YOUR_X_API_KEY" | gcloud secrets create X_API_KEY --data-file=-
   echo -n "YOUR_SF_CLIENT_ID" | gcloud secrets create SF_CLIENT_ID --data-file=-
   echo -n "YOUR_SF_CLIENT_SECRET" | gcloud secrets create SF_CLIENT_SECRET --data-file=-
   ```
3. Grant Cloud Run service account access to Google Drive folders.
4. Run `deploy.sh`.

### Local development steps
1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill values.
3. `uvicorn main:app --reload`

### Sample curl command
```bash
curl -X POST https://your-service-url/generate-invoice \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_X_API_KEY" \
  -d '{
    "uid": "UID123",
    "nbfcName": "Test NBFC",
    "loanTenure": "12 months",
    "taxableAmount": "1000",
    "loanAmount": "10000",
    "invoiceNumber": "INV-001",
    "billTo": "John Doe",
    "shipTo": "John Doe",
    "state": "Telangana",
    "loanApplicantName": "John Doe",
    "placeOfSupply": "Hyderabad",
    "invoiceDate": "2023-10-27",
    "itemName": "Processing Fee",
    "rate": "1000",
    "recordId": "SF_RECORD_ID"
  }'
```

### Salesforce fields updated

| Field API Name | Value Set | Description |
|---|---|---|
| `Invoice_Links__c` | PDF URL | Link to the generated PDF in Google Drive. |
| `Invoice_status__c` | "Invoice Generated" | Updated status after generation. |
| `Follow_Up_Date_Time_NBFC__c` | Timestamp | UTC timestamp of generation. |
