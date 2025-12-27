import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
from supabase import create_client, Client
import google.generativeai as genai
import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

load_dotenv()

# Environment Variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_CHANNEL_NAME = os.getenv("TWITCH_CHANNEL_NAME")  # e.g., "your_channel"

app = FastAPI()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')

# YouTube OAuth Scopes
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]

class BroadcastRequest(BaseModel):
    message: str
    username: str


# ========== TWITCH INTEGRATION ==========

def get_twitch_token():
    """Get OAuth token for Twitch API"""
    url = "https://id.twitch.tv/oauth2/token"
    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials"
    }
    response = requests.post(url, params=params)
    return response.json().get("access_token")


async def send_to_twitch(message: str):
    """Send message to Twitch chat (requires bot account setup)"""
    try:
        # Note: This uses Twitch's IRC interface via a simple approach
        # For production, use twitchio library with a bot account
        # This is a placeholder showing the structure
        
        token = get_twitch_token()
        # Twitch Chat API requires a bot account with chat:edit scope
        # The client_credentials flow doesn't grant chat access
        # You'll need to use OAuth with a user token instead
        
        print(f"[Twitch] Would send: {message}")
        # TODO: Implement with proper bot OAuth token
        return {"success": True, "platform": "twitch"}
    except Exception as e:
        print(f"[Twitch Error] {str(e)}")
        return {"success": False, "error": str(e)}


# ========== YOUTUBE INTEGRATION ==========

def get_youtube_credentials():
    """Get or refresh YouTube OAuth credentials"""
    creds = None
    
    # Check if we have saved credentials
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    
    # If no valid credentials, do the OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'client_secret.json', YOUTUBE_SCOPES)
            creds = flow.run_local_server(port=8080)
        
        # Save credentials for next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    
    return creds


async def send_to_youtube(message: str):
    """Send message to YouTube Live Chat"""
    try:
        creds = get_youtube_credentials()
        youtube = build('youtube', 'v3', credentials=creds)
        
        # Step 1: Get the active live broadcast
        broadcasts = youtube.liveBroadcasts().list(
            part="snippet",
            broadcastStatus="active",
            maxResults=1
        ).execute()
        
        if not broadcasts.get('items'):
            print("[YouTube] No active broadcast found")
            return {"success": False, "error": "No active stream"}
        
        # Step 2: Get the live chat ID
        live_chat_id = broadcasts['items'][0]['snippet']['liveChatId']
        
        # Step 3: Insert the message
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
        
        print(f"[YouTube] Sent: {message}")
        return {"success": True, "platform": "youtube"}
        
    except Exception as e:
        print(f"[YouTube Error] {str(e)}")
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


@app.get("/test-youtube")
async def test_youtube():
    """Test endpoint to verify YouTube auth is working"""
    try:
        creds = get_youtube_credentials()
        youtube = build('youtube', 'v3', credentials=creds)
        
        broadcasts = youtube.liveBroadcasts().list(
            part="snippet",
            broadcastStatus="active",
            maxResults=1
        ).execute()
        
        return {"status": "connected", "active_streams": len(broadcasts.get('items', []))}
    except Exception as e:
        return {"status": "error", "error": str(e)}