#!/usr/bin/env python3
"""Append new env vars to .env if not already present."""
import os

additions = {
    "GOOGLE_CLIENT_ID":     "212240349909-op1tjqvqpt7ktoe3d56rfi2tgu3l41n3.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "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",
    "GOOGLE_REDIRECT_URI":  "https://stockmarketview.duckdns.org/auth/google/callback",
    "SESSION_SECRET":       os.urandom(32).hex(),
    "TWILIO_ACCOUNT_SID":   "",  # fill in after Twilio signup
    "TWILIO_AUTH_TOKEN":    "",  # fill in after Twilio signup
    "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886",  # sandbox number
}

env_path = "/opt/marketview/.env"
with open(env_path, 'r') as f:
    existing = f.read()

to_add = []
for k, v in additions.items():
    if k not in existing:
        to_add.append(f"{k}={v}")

if to_add:
    with open(env_path, 'a') as f:
        f.write("\n# ── Portfolio / Google OAuth / Twilio ──\n")
        for line in to_add:
            f.write(line + "\n")
    print(f"✅ Added {len(to_add)} vars to .env")
else:
    print("✅ All vars already present")
