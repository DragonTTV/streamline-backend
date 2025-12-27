import os
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import google.generativeai as genai

load_dotenv()


SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


app = FastAPI()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-lite')


class BroadcastRequest(BaseModel):
    message: str
    username: str



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
        # A. Fetch chat logs (Newest first)
        response = supabase.table("chat_messages")\
            .select("username, message_text")\
            .order("created_at", desc=True)\
            .limit(50)\
            .execute()
        
        data = response.data
        if not data:
            return {"summary": "Chat has been quiet. Nothing to report!"}

        # B. Format for AI
        # We reverse it so the AI reads chronologically (Old -> New)
        chat_log = "\n".join([f"{msg['username']}: {msg['message_text']}" for msg in reversed(data)])

        # C. Prompt Engineering
        prompt = (
            "You are a helpful assistant for a streamer. "
            "Here is the chat log from the last few minutes. "
            "Summarize what happened in 2-3 short, funny sentences. "
            "Highlight if anyone subscribed or if there was drama.\n\n"
            f"CHAT LOG:\n{chat_log}"
        )

        # D. Generate
        result = model.generate_content(prompt)
        return {"summary": result.text}

    except Exception as e:
        return {"summary": f"AI Brain Freeze: {str(e)}"}

@app.post("/broadcast")
async def broadcast_message(req: BroadcastRequest):
    """
    Receives a message from the App and 'Broadcasts' it.
    For now, it just saves it back to Supabase so it appears in the feed.
    Later, you add Twitch/YouTube API calls here.
    """
    try:
        # 1. (Future) Send to Twitch API...
        # 2. (Future) Send to YouTube API...

        # 3. Save to Supabase (so it shows in our own app)
        data = {
            "username": req.username,
            "message_text": req.message,
            "platform": "twitch", # Defaulting to Twitch for MVP
            "is_subscriber": True # The streamer is always a sub!
        }
        supabase.table("chat_messages").insert(data).execute()
        
        return {"status": "sent", "message": req.message}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))