import os
from typing import Annotated
from fastapi import FastAPI, Depends, HTTPException, status, Request, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client, PostgrestAPIError
from dotenv import load_dotenv
from jose import jwt, JWTError

load_dotenv()

origins = [
    "http://localhost",
    "http://localhost:5173",
]


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"Connection added. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"Connection removed. Total connections: {len(self.active_connections)}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except Exception as e:
            print(f"Error sending personal message: {e}")

    async def broadcast(self, message: str):
        print(f"Broadcasting to {len(self.active_connections)} connections: {message}")
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"Error broadcasting to connection: {e}")
                disconnected.append(connection)
        # Clean up disconnected connections
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()

@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    print(f"WebSocket connection attempt from client: {client_id}")
    await manager.connect(websocket)
    print(f"Client {client_id} connected to WebSocket")
    try:
        await manager.broadcast(f"User {client_id} joined the chat")
        while True:
            data = await websocket.receive_text()
            print(f"Received from {client_id}: {data}")
            message = f"User {client_id}: {data}"
            await manager.broadcast(message)
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print(f"Client {client_id} disconnected")
        try:
            await manager.broadcast(f"User {client_id} left the chat")
        except Exception as e:
            print(f"Error broadcasting disconnect: {e}")
    except Exception as e:
        print(f"WebSocket error for {client_id}: {e}")
        manager.disconnect(websocket)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# This is still useful if you plan to interact with the Supabase DB elsewhere
supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")
supabase_service_role_key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(supabase_url, supabase_key)
supabase_service: Client = create_client(
    supabase_url, supabase_service_role_key)


# Get the JWT secret from environment variables
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("SUPABASE_JWT_SECRET not set in .env file")


def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = supabase.auth.get_claims(token).get("claims", {})
        return payload
    except JWTError:
        raise credentials_exception


@app.get("/public-route")
async def public_route():
    return {"message": "This is a public route."}


@app.get("/protected-route")
async def protected_route(user: dict = Depends(get_current_user)):
    # The 'user' object is now the decoded JWT payload
    return {"message": "This is a protected route.", "user_payload": user}


@app.get("/api/check-onboarded")
async def check_onboarded(user: dict = Depends(get_current_user)):
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=400, detail="User ID not found in token")

    response = supabase.from_("profiles").select(
        "username").eq("user_id", user_id).single().execute()
    # if response.error:
    #     raise HTTPException(status_code=500, detail="Database query failed")

    onboarded = bool(response.data["username"])
    return {"isOnboarded": onboarded}


@app.get("/api/users/check-username-availability")
async def check_username_availability(username: str):
    response = supabase.from_("profiles").select(
        "username").eq("username", username).execute()
    # if response.error and response.status_code != 406:  # 406 means no rows found
    #     raise HTTPException(status_code=500, detail="Database query failed")
    print("Database response for username check:", response)  # Debugging line

    is_available = len(response.data) == 0
    # Debugging line
    print(f"Username '{username}' availability: {is_available}")
    return {"isAvailable": is_available}


@app.patch('/api/users/me/set-username')
async def set_username(request: Request, token: Annotated[str, Depends(oauth2_scheme)]):
    data = await request.json()
    username = data.get("username")
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    payload = get_current_user(token)

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=400, detail="User ID not found in token")

    try:
        response = supabase_service.table("profiles").update(
            {"username": username}).eq("user_id", user_id).execute()
    # if response.error:
    #     raise HTTPException(status_code=500, detail="Failed to update username")
    except PostgrestAPIError as e:
        raise HTTPException(status_code=500, detail=f"API Error: {str(e)}")

    return {"message": "Username updated successfully"}


@app.post('/api/create-project')
async def create_new_project(request: Request, token: Annotated[str, Depends(oauth2_scheme)]):
    data = await request.json()
    user = get_current_user(token).get("sub")
    project_name = data.get("project_name")
    print("Received project creation request with data:", data)  # Debugging line
    if not project_name:
        raise HTTPException(status_code=400, detail="Project name is required")
    response = supabase_service.rpc('create_project', {
        'project_name': project_name,
        'project_owner': user
    }).execute()
    print("Database response for project creation:", response)  # Debugging line
    return {"message": "New project created successfully"}


@app.get('/api/get-projects')
async def get_user_projects(token: Annotated[str, Depends(oauth2_scheme)]):
    user = get_current_user(token).get("sub")
    matches = supabase_service.table("project_member").select(
        "*").eq("user_id", user).execute()
    response = []
    for record in matches.data:
        project = supabase_service.table("project").select(
            "*").eq("project_id", record["project_id"]).single().execute()
        project.data["role"] = record["role"]
        # fetch and attach the owner's username so frontend can show the owner
        owner_username = None
        owner_id = project.data.get("owner_id")
        if owner_id:
            owner_resp = supabase_service.table("profiles").select(
                "username").eq("user_id", owner_id).single().execute()
            if owner_resp.data and "username" in owner_resp.data:
                owner_username = owner_resp.data["username"]
        project.data["owner_username"] = owner_username
        response.append(project.data)
    return {"projects": response}


@app.post('/api/add-collaborator')
async def add_collaborator(request: Request, token: Annotated[str, Depends(oauth2_scheme)]):
    data = await request.json()
    user = get_current_user(token).get("sub")
    project_id = data.get("project_id")
    collaborator_username = data.get("collaborator_username")
    collaborator_role = data.get("role") or "viewer"
    print("Received add collaborator request with data:", data)  # Debugging line
    if not project_id or not collaborator_username:
        raise HTTPException(
            status_code=400, detail="Project ID and collaborator username are required")
    if collaborator_role not in ["viewer", "editor"]:
        raise HTTPException(
            status_code=400, detail="Invalid collaborator role")
    owner_response = supabase_service.table("project").select(
        "owner_id").eq("project_id", project_id).single().execute()
    if owner_response.data["owner_id"] != user:
        raise HTTPException(
            status_code=403, detail="Only the project owner can add collaborators")
    response = supabase_service.table("project_member").insert({
        "project_id": project_id,
        "user_id": supabase_service.table("profiles").select(
            "user_id").eq("username", collaborator_username).single().execute().data["user_id"],
        "role": collaborator_role
    }).execute()
    print("Database response for adding collaborator:",
          response)  # Debugging line
    return {"message": "Collaborator added successfully"}