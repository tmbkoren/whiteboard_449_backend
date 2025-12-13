import json
import os
import asyncio
from typing import Annotated

from fastapi import (
    FastAPI, Depends, HTTPException, status,
    Request, WebSocket, WebSocketDisconnect
)
from fastapi.security import OAuth2PasswordBearer
from fastapi.middleware.cors import CORSMiddleware

from supabase import create_client, Client, PostgrestAPIError
from dotenv import load_dotenv
import redis.asyncio as redis

load_dotenv()

# --------------------------------------------------
# APP + CORS
# --------------------------------------------------

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

# --------------------------------------------------
# REDIS
# --------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL not set")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)
REDIS_CHANNEL = "whiteboard_events"

# --------------------------------------------------
# CONNECTION MANAGER (LOCAL ONLY)
# --------------------------------------------------


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str):
        await websocket.accept()
        self.active_connections.setdefault(room_id, []).append(websocket)
        print(
            f"Connection added to room {room_id}. "
            f"Total connections in room: {len(self.active_connections[room_id])}"
        )
        print("INSTANCE ID:", id(self))

    def disconnect(self, websocket: WebSocket, room_id: str):
        if room_id in self.active_connections:
            if websocket in self.active_connections[room_id]:
                self.active_connections[room_id].remove(websocket)
            if not self.active_connections[room_id]:
                del self.active_connections[room_id]

        print(
            f"Connection removed from room {room_id}. "
            f"Total connections in room: {len(self.active_connections.get(room_id, []))}"
        )

    async def broadcast_local(self, message: str, room_id: str):
        connections = self.active_connections.get(room_id, [])
        print(
            f"LOCAL broadcast to {len(connections)} clients in room {room_id}")

        disconnected = []
        for ws in connections:
            try:
                await ws.send_text(message)
            except Exception as e:
                print(f"Error broadcasting: {e}")
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(ws, room_id)


manager = ConnectionManager()

# --------------------------------------------------
# REDIS PUB / SUB
# --------------------------------------------------


async def publish(room_id: str, payload: dict):
    await redis_client.publish(
        REDIS_CHANNEL,
        json.dumps({
            "room_id": room_id,
            "payload": payload
        })
    )


@app.on_event("startup")
async def start_redis_listener():
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(REDIS_CHANNEL)

    async def reader():
        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue

            data = json.loads(msg["data"])
            room_id = data["room_id"]
            payload = data["payload"]

            await manager.broadcast_local(
                json.dumps(payload),
                room_id
            )

    asyncio.create_task(reader())

# --------------------------------------------------
# AUTH + SUPABASE
# --------------------------------------------------

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

supabase_url: str = os.environ.get("SUPABASE_URL")
supabase_key: str = os.environ.get("SUPABASE_KEY")
supabase_service_role_key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

supabase: Client = create_client(supabase_url, supabase_key)
supabase_service: Client = create_client(
    supabase_url, supabase_service_role_key
)

JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("SUPABASE_JWT_SECRET not set")


def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = supabase.auth.get_claims(token).get("claims", {})
        return payload
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        )

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


@app.get("/protected-route")
async def protected_route(user: dict = Depends(get_current_user)):
    # The 'user' object is now the decoded JWT payload
    return {"message": "This is a protected route.", "user_payload": user}


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


@app.get('/api/get-project/{project_id}')
async def get_project_details(project_id: str, token: Annotated[str, Depends(oauth2_scheme)]):
    # I need to return project details along with the user's role in that project
    user = get_current_user(token).get("sub")
    membership = supabase_service.table("project_member").select(
        "*").eq("user_id", user).eq("project_id", project_id).single().execute()
    if not membership.data:
        raise HTTPException(
            status_code=403, detail="You do not have access to this project")
    project = supabase_service.table("project").select(
        "*").eq("project_id", project_id).single().execute()
    project_data = project.data
    project_data["role"] = membership.data["role"]
    return {"project": project_data}


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


@app.post('/api/create-whiteboard')
async def create_whiteboard(request: Request, token: Annotated[str, Depends(oauth2_scheme)]):
    data = await request.json()
    user = get_current_user(token).get("sub")
    project_id = data.get("project_id")
    whiteboard_name = data.get("whiteboard_name")
    print("Received whiteboard creation request with data:", data)  # Debugging line
    if not project_id or not whiteboard_name:
        raise HTTPException(
            status_code=400, detail="Project ID and whiteboard name are required")
    membership = supabase_service.table("project_member").select(
        "*").eq("user_id", user).eq("project_id", project_id).single().execute()
    if not membership.data or membership.data["role"] not in ["editor", "owner"]:
        raise HTTPException(
            status_code=403, detail="You do not have permission to add whiteboards to this project")
    response = supabase_service.rpc('create_whiteboard', {
        'project_id': project_id,
        'whiteboard_name': whiteboard_name
    }).execute()
    print("Database response for whiteboard creation:",
          response)  # Debugging line
    return {"message": "Whiteboard created successfully"}


@app.get('/api/get-whiteboards/{project_id}')
async def get_whiteboards(project_id: str, token: Annotated[str, Depends(oauth2_scheme)]):
    user = get_current_user(token).get("sub")
    membership = supabase_service.table("project_member").select(
        "*").eq("user_id", user).eq("project_id", project_id).single().execute()
    if not membership.data:
        raise HTTPException(
            status_code=403, detail="You do not have access to this project's whiteboards")
    response = supabase_service.rpc('getprojectwhiteboards', {
        'lookup_project_id': project_id
    }).execute()
    return {"whiteboards": response.data}


@app.get('/api/get-whiteboard/{whiteboard_id}')
async def get_whiteboard_details(whiteboard_id: str, token: Annotated[str, Depends(oauth2_scheme)]):
    user = get_current_user(token).get("sub")
    whiteboard_response = supabase_service.table("whiteboards").select(
        "*").eq("id", whiteboard_id).single().execute()
    if not whiteboard_response.data:
        raise HTTPException(
            status_code=404, detail="Whiteboard not found")
    project_id_response = supabase_service.table("project_whiteboard").select(
        "project_id").eq("whiteboard_id", whiteboard_id).single().execute()
    if not project_id_response.data:
        raise HTTPException(
            status_code=404, detail="Whiteboard project relationship not found")
    project_id = project_id_response.data["project_id"]
    membership = supabase_service.table("project_member").select(
        "*").eq("user_id", user).eq("project_id", project_id).single().execute()
    if not membership.data:
        raise HTTPException(
            status_code=403, detail="You do not have access to this whiteboard")
    chat_data = supabase_service.rpc('getlastmessages', {
        'lookup_whiteboard_id': whiteboard_id,
    }).execute()
    return {"whiteboard": whiteboard_response.data, "chat_messages": chat_data.data}


@app.websocket("/ws/whiteboard/{whiteboard_id}/{client_id}")
async def whiteboard_websocket(
    websocket: WebSocket,
    whiteboard_id: str,
    client_id: str
):
    print(f"WebSocket connection attempt for whiteboard: {whiteboard_id}")
    await manager.connect(websocket, whiteboard_id)

    await publish(whiteboard_id, {
        "type": "USER_JOINED",
        "client_id": client_id,
        "whiteboard_id": whiteboard_id,
    })

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # -------- WHITEBOARD UPDATE --------
            if msg_type == "UPDATE_WHITEBOARD":
                if "elements" in data:
                    supabase_service.table("whiteboards").update({
                        "elements": data["elements"]
                    }).eq("id", whiteboard_id).execute()

                if "appState" in data:
                    supabase_service.table("whiteboards").update({
                        "app_state": data["appState"]
                    }).eq("id", whiteboard_id).execute()

                await publish(whiteboard_id, data)

            # -------- CHAT MESSAGE --------
            elif msg_type == "NEW_MESSAGE":
                new_msg = supabase_service.table("chat_messages").insert({
                    "whiteboard_id": whiteboard_id,
                    "sender_id": data["sender_id"],
                    "content": data["message"],
                }).execute()

                sender_username = supabase_service.table("profiles") \
                    .select("username") \
                    .eq("user_id", data["sender_id"]) \
                    .single() \
                    .execute() \
                    .data["username"]

                await publish(whiteboard_id, {
                    "type": "NEW_MESSAGE",
                    "message": {
                        "sender_id": data["sender_id"],
                        "sender_username": sender_username,
                        "content": data["message"],
                        "sent_at": new_msg.data[0]["sent_at"]
                    }
                })

    except WebSocketDisconnect:
        manager.disconnect(websocket, whiteboard_id)
        await publish(whiteboard_id, {
            "type": "USER_LEFT",
            "client_id": client_id,
            "whiteboard_id": whiteboard_id,
        })

    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket, whiteboard_id)
