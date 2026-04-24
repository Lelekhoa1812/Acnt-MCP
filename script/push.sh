#!/usr/bin/env bash

set -euo pipefail

# Single-image Docker build and push script for Horse app.
# Target image: <registry>/hth-mcp:<tag>

REGISTRY="${REGISTRY:-crairagapidevase.azurecr.io}"
IMAGE_NAME="hth-mcp/hth-harmonise-mcp"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCKERFILE_PATH="${PROJECT_ROOT}/Dockerfile"
BUILD_DIR="${PROJECT_ROOT}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=========================================="
echo "Horse App Docker Build and Push"
echo "=========================================="
echo ""

if [[ ! -f "${DOCKERFILE_PATH}" ]]; then
  echo -e "${RED}Error: Dockerfile not found at ${DOCKERFILE_PATH}${NC}"
  exit 1
fi

current_date="$(date +"%d-%m-%Y")"
echo "Current date: ${current_date}"
read -r -p "Version (format x.x.x, e.g. 1.0.0): " version

if [[ ! "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo -e "${YELLOW}Warning: version is not x.x.x, continuing...${NC}"
fi

tag_version="${current_date}-${version}"
local_image="${IMAGE_NAME}:${tag_version}"
registry_image="${REGISTRY}/${IMAGE_NAME}:${tag_version}"

echo ""
echo "=========================================="
echo "Configuration"
echo "=========================================="
echo "  Local Image: ${local_image}"
echo "  Registry Image: ${registry_image}"
echo "  Build Directory: ${BUILD_DIR}"
echo ""
read -r -p "Press Enter to continue or Ctrl+C to cancel..."
echo ""

if ! command -v docker >/dev/null 2>&1; then
  echo -e "${RED}Error: Docker is not installed or not in PATH.${NC}"
  exit 1
fi

# If this is an Azure Container Registry, try az acr login.
if [[ "${REGISTRY}" == *.azurecr.io ]]; then
  if ! command -v az >/dev/null 2>&1; then
    echo -e "${RED}Error: Azure CLI required for ${REGISTRY}, but not found.${NC}"
    exit 1
  fi

  acr_name="${REGISTRY%%.azurecr.io}"
  if ! az account show >/dev/null 2>&1; then
    echo -e "${YELLOW}Not logged in to Azure. Starting az login...${NC}"
    az login
  fi

  echo "Logging in to Azure Container Registry: ${acr_name}"
  az acr login --name "${acr_name}"
fi

echo ""
echo "=========================================="
echo "Step 1: Build image"
echo "=========================================="
echo ""

cd "${BUILD_DIR}"
docker buildx build \
  --platform linux/amd64 \
  -f "${DOCKERFILE_PATH}" \
  -t "${local_image}" \
  -t "${registry_image}" \
  --load \
  .

echo -e "${GREEN}Build complete${NC}"
echo ""

echo "=========================================="
echo "Step 2: Push image"
echo "=========================================="
echo ""

docker push "${registry_image}"

echo ""
echo "=========================================="
echo -e "${GREEN}Done${NC}"
echo "=========================================="
echo "Pushed: ${registry_image}"
