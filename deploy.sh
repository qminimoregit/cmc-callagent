#!/bin/bash
set -e

REGION="ap-south-1"
ACCOUNT_ID="526338062139"
REPO_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/cmc-assistant"
CLUSTER_NAME="cmc-cluster"
SERVICE_NAME="cmc-service"

echo "Logging in to Amazon ECR..."
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REPO_URI

echo "Building the Docker image..."
# Using --platform linux/amd64 to ensure compatibility with AWS Fargate if you are building on an Apple Silicon (M1/M2/M3) Mac
docker build --platform linux/amd64 -t cmc-assistant .

echo "Tagging the Docker image..."
docker tag cmc-assistant:latest $REPO_URI:latest

echo "Pushing the Docker image to ECR..."
docker push $REPO_URI:latest

echo "Registering new ECS task definition..."
aws ecs register-task-definition --cli-input-json file://task-definition.json

echo "Updating ECS service to use the new image..."
aws ecs update-service --cluster $CLUSTER_NAME --service $SERVICE_NAME --task-definition cmc-task --force-new-deployment

echo "Deployment initiated successfully!"
