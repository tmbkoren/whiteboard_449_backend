# main.py or a separate firebase_config.py
import firebase_admin
from firebase_admin import credentials, auth
import os
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security.oauth2 import OAuth2PasswordBearer
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware

origins = [
    "http://localhost",
    "http://localhost:5173",
]

# Path to your Firebase service account key JSON file
# For production, consider storing this securely (e.g., in Secret Manager and loading it)
# For local development, you can place it in your project root or use an environment variable
SERVICE_ACCOUNT_KEY_PATH = os.getenv(
    "FIREBASE_SERVICE_ACCOUNT_KEY_PATH", "./serviceAccountKey.json")

# Initialize Firebase Admin SDK
try:
    cred = credentials.Certificate(SERVICE_ACCOUNT_KEY_PATH)
    firebase_admin.initialize_app(cred)
    print("Firebase Admin SDK initialized successfully.")
except Exception as e:
    print(f"Error initializing Firebase Admin SDK: {e}")
    # Depending on your deployment, you might want to exit or handle this differently
    # For Cloud Run, ensure the service account key is accessible.

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# tokenUrl is for docs, not actual endpoint
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


async def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        # Verify the Firebase ID token
        decoded_token = auth.verify_id_token(token)
        return decoded_token
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication credentials: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/protected-route")
async def protected_route(current_user: dict = Depends(get_current_user)):
    return {"message": "You accessed a protected route!", "user_uid": current_user["uid"], "user_email": current_user["email"]}

# Example of a public route


@app.get("/public-route")
async def public_route():
    return {"message": "This is a public route."}

# You would also have your FastAPI endpoints for whiteboard functionality here
# e.g., @app.post("/whiteboard/{board_id}/draw")
