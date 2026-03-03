#!/bin/bash

VPS_IP="66.23.199.133"
VPS_USER="root"
VPS_PASS="rishu"
TARGET_DIR="/root/test"

echo "Rebuilding $TARGET_DIR on VPS..."

sshpass -p "$VPS_PASS" ssh -o StrictHostKeyChecking=no $VPS_USER@$VPS_IP \
"rm -rf $TARGET_DIR && mkdir -p $TARGET_DIR"

echo "Syncing local project to VPS..."

sshpass -p "$VPS_PASS" rsync -avz --delete \
--exclude 'deploy.sh' \
./ $VPS_USER@$VPS_IP:$TARGET_DIR/

echo "Deployment completed successfully."