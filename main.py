import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle
import pytchat
from twitchAPI.twitch import Twitch
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.type import AuthScope, ChatEvent
from twitchAPI.chat import Chat, EventData, ChatMessage

load_dotenv()

# Environment Variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME")  # e.g., "your_channel"
YOUTUBE_VIDEO_ID = os.getenv("YOUTUBE_VIDEO_ID")  # The live stream video ID

app = FastAPI()

# CORS Middleware to prevent 405 errors from Flutter
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with your Flutter app's domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

# YouTube OAuth Scopes
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

# Global references for chat clients
twitch_chat = None
youtube_chat_listener = None

class BroadcastRequest(BaseModel):
    message: str
    username: str


# ========== YOUTUBE INTEGRATION (HYBRID) ==========

def get_youtube_credentials():
    """Get or refresh YouTube OAuth credentials"""
    creds = None
    
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secret.json', YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=8080)
        
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    
    return creds


async def youtube_listener():
    """Background task: Listen to YouTube chat using pytchat (quota-free)"""
    global youtube_chat_listener
    
    if not YOUTUBE_VIDEO_ID:
        print("[YouTube Listener] No VIDEO_ID set. Skipping.")
        return
    
    try:
        print(f"[YouTube Listener] Starting for video: {YOUTUBE_VIDEO_ID}")
        youtube_chat_listener = pytchat.create(video_id=YOUTUBE_VIDEO_ID)
        
        while youtube_chat_listener.is_alive():
            for chat in youtube_chat_listener.get().sync_items():
                # Save to Supabase
                data = {
                    "username": chat.author.name,
                    "message_text": chat.message,
                    "platform": "youtube",
                    "is_subscriber": chat.author.isChatSponsor
                }
                supabase.table("chat_messages").insert(data).execute()
                print(f"[YouTube] {chat.author.name}: {chat.message}")
            
            await asyncio.sleep(0.5)  # Small delay to prevent CPU spin
            
    except Exception as e:
        print(f"[YouTube Listener Error] {str(e)}")


async def send_to_youtube(message: str):
    """Send message to YouTube Live Chat using official API"""
    try:
        creds = get_youtube_credentials()
        youtube = build('youtube', 'v3', credentials=creds)
        
        # Get active broadcast
        broadcasts = youtube.liveBroadcasts().list(
            part="snippet",
            broadcastStatus="active",
            maxResults=1
        ).execute()
        
        if not broadcasts.get('items'):
            print("[YouTube Send] No active broadcast found")
            return {"success": False, "error": "No active stream"}
        
        live_chat_id = broadcasts['items'][0]['snippet']['liveChatId']
        
        # Insert message
        youtube.liveChatMessages().insert(
            part="snippet",
            body={
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {
                        "messageText": message
                    }
                }
            }
        ).execute()
        
        print(f"[YouTube Send] ✓ {message}")
        return {"success": True, "platform": "youtube"}
        
    except Exception as e:
        print(f"[YouTube Send Error] {str(e)}")
        return {"success": False, "error": str(e)}


# ========== TWITCH INTEGRATION (WebSocket) ==========

async def on_twitch_ready(ready_event: EventData):
    """Called when Twitch chat is ready"""
    print(f"[Twitch] Connected as {ready_event.chat.username}")
    await ready_event.chat.join_room(TWITCH_CHANNEL_NAME)


async def on_twitch_message(msg: ChatMessage):
    """Called when a Twitch message arrives"""
    # Save to Supabase
    data = {
        "username": msg.user.name,
        "message_text": msg.text,
        "platform": "twitch",
        "is_subscriber": msg.user.subscriber
    }
    supabase.table("chat_messages").insert(data).execute()
    print(f"[Twitch] {msg.user.name}: {msg.text}")


async def twitch_listener():
    """Background task: Listen to Twitch chat using WebSocket"""
    global twitch_chat
    
    try:
        print("[Twitch Listener] Starting...")
        
        # Authenticate
        twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        auth = UserAuthenticator(twitch, [AuthScope.CHAT_READ, AuthScope.CHAT_EDIT])
        token, refresh_token = await auth.authenticate()
        await twitch.set_user_authentication(token, [AuthScope.CHAT_READ, AuthScope.CHAT_EDIT])
        
        # Create chat client
        twitch_chat = await Chat(twitch)
        
        # Register event handlers
        twitch_chat.register_event(ChatEvent.READY, on_twitch_ready)
        twitch_chat.register_event(ChatEvent.MESSAGE, on_twitch_message)
        
        # Start listening
        twitch_chat.start()
        
        # Keep alive
        while True:
            await asyncio.sleep(1)
            
    except Exception as e:
        print(f"[Twitch Listener Error] {str(e)}")


async def send_to_twitch(message: str):
    """Send message to Twitch chat"""
    global twitch_chat
    
    try:
        if twitch_chat is None:
            return {"success": False, "error": "Twitch chat not initialized"}
        
        await twitch_chat.send_message(TWITCH_CHANNEL_NAME, message)
        print(f"[Twitch Send] ✓ {message}")
        return {"success": True, "platform": "twitch"}
        
    except Exception as e:
        print(f"[Twitch Send Error] {str(e)}")
        return {"success": False, "error": str(e)}


# ========== BACKGROUND TASK WRAPPER ==========

async def broadcast_to_platforms(message: str):
    """Fire-and-forget broadcast to both platforms"""
    results = await asyncio.gather(
        send_to_twitch(message),
        send_to_youtube(message),
        return_exceptions=True
    )
    print(f"[Broadcast Results] {results}")


# ========== STARTUP EVENT ==========

@app.on_event("startup")
async def startup_event():
    """Start background listeners when FastAPI starts"""
    asyncio.create_task(youtube_listener())
    asyncio.create_task(twitch_listener())
    print("🚀 [StreamLine Brain] All listeners started!")


# ========== API ENDPOINTS ==========

@app.get("/")
def read_root():
    return {"status": "StreamLine Brain is Online 🧠"}


@app.get("/summarize")
async def get_summary():
    """
    1. Fetches the last 50 chat messages from Supabase.
    2. Sends them to Gemini to summarize.
    """
    try:
        response = supabase.table("chat_messages")\
            .select("username, message_text")\
            .order("created_at", desc=True)\
            .limit(50)\
            .execute()
        
        data = response.data
        if not data:
            return {"summary": "Chat has been quiet. Nothing to report!"}

        chat_log = "\n".join([f"{msg['username']}: {msg['message_text']}" for msg in reversed(data)])

        prompt = (
            "You are a helpful assistant for a streamer. "
            "Here is the chat log from the last few minutes. "
            "Summarize what happened in 2-3 short, funny sentences. "
            "Highlight if anyone subscribed or if there was drama.\n\n"
            f"CHAT LOG:\n{chat_log}"
        )

        result = model.generate_content(prompt)
        return {"summary": result.text}

    except Exception as e:
        return {"summary": f"AI Brain Freeze: {str(e)}"}


@app.post("/broadcast")
async def broadcast_message(req: BroadcastRequest, background_tasks: BackgroundTasks):
    """
    Receives a message from the App and broadcasts it to:
    1. Twitch Chat
    2. YouTube Live Chat
    3. Supabase (so it appears in the StreamLine feed)
    """
    try:
        # Fire-and-forget to platforms (non-blocking)
        background_tasks.add_task(broadcast_to_platforms, req.message)
        
        # Save to Supabase immediately
        data = {
            "username": req.username,
            "message_text": req.message,
            "platform": "streamline",
            "is_subscriber": True
        }
        supabase.table("chat_messages").insert(data).execute()
        
        return {"status": "sent", "message": req.message}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
async def health_check():
    """Check if listeners are running"""
    return {
        "youtube_listener": youtube_chat_listener is not None and youtube_chat_listener.is_alive() if youtube_chat_listener else False,
        "twitch_listener": twitch_chat is not None,
        "supabase": "connected",
        "gemini": "connected"
    }