#!/bin/bash
# ============================================================
# deploy.sh — Deploy Invoice Service to GCP Cloud Run
# Run this once from your terminal after installing gcloud CLI
# ============================================================

# ── CONFIG: fill these before running ───────────────────────
PROJECT_ID="your-gcp-project-id"           # GCP project ID
REGION="asia-south1"                        # Mumbai — closest to Hyderabad
SERVICE_NAME="invoice-service"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

# ── STEP 1: Set project ──────────────────────────────────────
echo "▶ Setting GCP project..."
gcloud config set project $PROJECT_ID

# ── STEP 2: Enable required APIs ────────────────────────────
echo "▶ Enabling APIs..."
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  drive.googleapis.com

# ── STEP 3: Build and push Docker image ─────────────────────
echo "▶ Building Docker image..."
gcloud builds submit --tag $IMAGE

# ── STEP 4: Store secrets in Secret Manager ─────────────────
echo "▶ Creating secrets..."

# Run these manually with your actual values:
# echo -n "your_x_api_key"       | gcloud secrets create X_API_KEY --data-file=-
# echo -n "your_sf_client_id"    | gcloud secrets create SF_CLIENT_ID --data-file=-
# echo -n "your_sf_client_secret"| gcloud secrets create SF_CLIENT_SECRET --data-file=-

# ── STEP 5: Deploy to Cloud Run ──────────────────────────────
echo "▶ Deploying to Cloud Run..."
gcloud run deploy $SERVICE_NAME \
  --image $IMAGE \
  --region $REGION \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --timeout 120 \
  --set-env-vars "\
GOOGLE_TEMPLATE_FILE_ID=1pvGURo7MtJSyqq3KkkNgk8TeG9oBxP6VZ-rUw3QIRXA,\
GOOGLE_DOCS_FOLDER_ID=1xLiHJvukOFpl9Q8dwXxsTMTWxjLvxTOg,\
GOOGLE_PDFS_FOLDER_ID=YOUR_PDFS_FOLDER_ID,\
SF_LOGIN_DOMAIN=https://computing-ability-6555.my.salesforce.com,\
SF_API_VERSION=61.0" \
  --set-secrets "\
X_API_KEY=X_API_KEY:latest,\
SF_CLIENT_ID=SF_CLIENT_ID:latest,\
SF_CLIENT_SECRET=SF_CLIENT_SECRET:latest"

echo ""
echo "✅ Deployed! Service URL:"
gcloud run services describe $SERVICE_NAME --region $REGION --format "value(status.url)"
