# AWS Setup Guide - GEMINI_API_KEY & Environment Variables

## ⚠️ Problem
Your Twilio calls are failing with:
```
GEMINI_API_KEY is not set in the environment.
```

The LLM cannot process caller requests without this API key.

---

## ✅ Solution: 3 Options

### Option 1: AWS Secrets Manager (Recommended for Production) 🔒

This is the **most secure** method. Secrets are never stored in code or task definitions.

#### Step 1: Add GEMINI_API_KEY to Secrets Manager

```bash
# From your local machine, run:
aws secretsmanager create-secret \
    --name cmc-agent/gemini-api-key \
    --secret-string "YOUR_ACTUAL_GEMINI_API_KEY" \
    --region ap-south-1
```

Or via AWS Console:
1. Go to **Secrets Manager** → **Store a new secret**
2. Select **Other type of secret**
3. Key: `GEMINI_API_KEY`
4. Value: `YOUR_ACTUAL_GEMINI_API_KEY`
5. Name: `cmc-agent/gemini-api-key`
6. Click **Store**

#### Step 2: Update ECS Task Definition

In your **ECS Task Definition JSON** (in task-definition.json or via AWS Console):

```json
{
  "name": "cmc-assistant",
  "image": "YOUR_ECR_IMAGE",
  "environment": [
    {"name": "BASE_URL", "value": "https://your-alb-dns.us-east-1.elb.amazonaws.com"},
    {"name": "ENVIRONMENT", "value": "production"},
    ...other vars...
  ],
  "secrets": [
    {
      "name": "GEMINI_API_KEY",
      "valueFrom": "arn:aws:secretsmanager:ap-south-1:ACCOUNT_ID:secret:cmc-agent/gemini-api-key:GEMINI_API_KEY::"
    }
  ]
}
```

**Get your ARN**: Go to Secrets Manager → Click on `cmc-agent/gemini-api-key` → Copy the ARN

#### Step 3: Grant ECS Task IAM Role Permission

Add this policy to your **ECS Task Execution Role** (e.g., `ecsTaskExecutionRole`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue"
      ],
      "Resource": [
        "arn:aws:secretsmanager:ap-south-1:ACCOUNT_ID:secret:cmc-agent/gemini-api-key*"
      ]
    }
  ]
}
```

Then update entrypoint.sh to fetch it:

```bash
#!/usr/bin/env bash
# scripts/entrypoint.sh
set -e

# Google credentials from Secrets Manager
if [ -n "$GOOGLE_CREDENTIALS_SECRET_ARN" ]; then
    echo "[entrypoint] Fetching Google credentials..."
    aws secretsmanager get-secret-value \
        --secret-id "$GOOGLE_CREDENTIALS_SECRET_ARN" \
        --query SecretString \
        --output text \
        > /tmp/google-credentials.json
    export GOOGLE_APPLICATION_CREDENTIALS=/tmp/google-credentials.json
fi

echo "[entrypoint] Starting application..."
exec "$@"
```

✅ **Done!** The `GEMINI_API_KEY` secret is automatically injected as an env var by ECS.

---

### Option 2: ECS Task Definition Environment Variables (Simpler, Less Secure)

If you want to keep it simple and don't have sensitive data concerns:

1. Go to **ECS** → **Task Definitions** → Select your task definition
2. Click **Create new revision**
3. Scroll to **Container Definitions** → Click your container
4. Scroll down to **Environment variables**
5. Add:
   - Key: `GEMINI_API_KEY`
   - Value: `YOUR_ACTUAL_GEMINI_API_KEY`
6. Click **Update Container** → **Create**
7. Go back to **ECS Clusters** → your cluster → **Services** → Update service with new task definition

⚠️ **Warning**: API keys are visible in plain text in AWS Console. Use Option 1 for production.

---

### Option 3: Parameter Store + Systems Manager

Similar to Secrets Manager but for simpler values:

1. Go to **Systems Manager** → **Parameter Store** → **Create parameter**
2. Name: `/cmc/gemini-api-key`
3. Type: **SecureString**
4. Value: `YOUR_ACTUAL_GEMINI_API_KEY`
5. Update Task Definition `secrets` section to use the Parameter Store ARN

---

## 📋 Verify It Works

After deploying, check logs:

```bash
# View ECS logs
aws logs tail /ecs/cmc-assistant --follow

# Look for this (success):
# "All API clients pre-warmed at startup ⚡"
# "src.llm — Gemini client initialized"

# NOT this (failure):
# "[WARNING] Client pre-warm failed: GEMINI_API_KEY is not set"
```

---

## 🔑 Get Your GEMINI_API_KEY

1. Go to https://aistudio.google.com/apikey
2. Sign in with your Google account
3. Click **Create API Key**
4. Copy the key
5. Store it securely (Option 1 above) or add to ECS Task Definition (Option 2 above)

---

## ✅ After Fixing

1. **Redeploy** your ECS service with the updated task definition
2. **Test a call** through Twilio
3. Check CloudWatch logs for success

The call should now:
1. ✅ Accept incoming call
2. ✅ Play greeting
3. ✅ Record language choice
4. ✅ **✅ Process with LLM (this was failing)**
5. ✅ Generate response
6. ✅ Play response back to caller

---

## 🐛 Troubleshooting

### Still getting "GEMINI_API_KEY is not set"?

- ❌ Did you restart the ECS service after updating the task definition?
  - Go to **ECS Cluster** → **Services** → **Force new deployment** ✅

- ❌ Is the secret/parameter ARN correct?
  - Run: `aws secretsmanager describe-secret --secret-id cmc-agent/gemini-api-key`
  - Copy the exact `ARN` value ✅

- ❌ Does the ECS Task Role have permission?
  - Go to **IAM Roles** → Find `ecsTaskExecutionRole`
  - Attach the policy above ✅

- ❌ Is `GOOGLE_APPLICATION_CREDENTIALS` still working?
  - Check logs for `"Google credentials written to /tmp/google-credentials.json"`
  - If not, run: `aws secretsmanager get-secret-value --secret-id $GOOGLE_CREDENTIALS_SECRET_ARN` ✅

---

## Other Environment Variables to Check

Make sure these are also set in your ECS Task Definition:

```
BASE_URL = https://your-alb-dns.us-east-1.elb.amazonaws.com
DATABASE_URL = postgresql://...
REDIS_URL = redis://cmc-redis.nfqscw.0001.aps1.cache.amazonaws.com:6379
TWILIO_ACCOUNT_SID = AC...
TWILIO_AUTH_TOKEN = ...
ESCALATION_NUMBER = +94...
S3_AUDIO_BUCKET = your-bucket-name
GOOGLE_CREDENTIALS_SECRET_ARN = arn:aws:secretsmanager:ap-south-1:...
```

---

## ✅ Also Fixed: Twilio StatusCallback Issue

The warning about `status_callback_event` has been fixed in `src/server.py`. This doesn't affect call functionality but now call status tracking will work correctly.
