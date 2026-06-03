import streamlit as st
import os
import io
import time
import base64
import re
import subprocess
import sys
from typing import Annotated, List, TypedDict, Union
from PIL import Image
from duckduckgo_search import DDGS
from playwright.sync_api import sync_playwright

# Optional stealth import
try:
    from playwright_stealth import stealth_sync
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

# --- 1. UTILS ---

def extract_intel_from_text(text: str):
    intel = {
        "Emails": list(set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text))),
        "Social Media": list(set(re.findall(r'(?:twitter\.com|linkedin\.com|instagram\.com|facebook\.com|github\.com)/[a-zA-Z0-9._-]+', text))),
        "Phones": list(set(re.findall(r'\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}', text)))
    }
    return intel

# --- 2. THE BROWSER (Resilient Version) ---

# Set a local path for playwright browsers to avoid permission/path issues on Streamlit Cloud
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(os.getcwd(), "pw-browsers")

class OSINTBrowser:
    def __init__(self, proxy=None):
        self.cleanup()
        self.browser = None
        self.pw = None
        self.page = None
        self.context = None
        
        # Streamlit Cloud Setup
        if "pw_ready" not in st.session_state:
            with st.status("🛠️ Setting up Browser Environment...", expanded=True) as status:
                try:
                    browser_path = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
                    if not os.path.exists(browser_path):
                        os.makedirs(browser_path)
                        
                    status.write("Installing Chromium binaries to local path...")
                    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
                    env = os.environ.copy()
                    
                    process = subprocess.run(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env
                    )
                    
                    if process.returncode == 0:
                        st.session_state.pw_ready = True
                        status.update(label="✅ Browser Environment Ready!", state="complete", expanded=False)
                    else:
                        error_detail = process.stderr or process.stdout
                        status.error(f"❌ Installation Failed (Exit {process.returncode})")
                        st.code(error_detail)
                        # Fallback attempt
                        status.write("Attempting fallback installation...")
                        subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], env=env)
                        st.session_state.pw_ready = True
                except Exception as e:
                    status.error(f"❌ Critical Setup Error: {e}")
                    st.session_state.pop("pw_ready", None)
                    return

        try:
            self.pw = sync_playwright().start()
            args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"]
            conf = {"headless": True, "args": args}
            if proxy: conf["proxy"] = {"server": proxy}
            
            self.browser = self.pw.chromium.launch(**conf)
            self.context = self.browser.new_context(viewport={"width": 1280, "height": 720})
            self.page = self.context.new_page()
            if STEALTH_AVAILABLE: stealth_sync(self.page)
            st.session_state.osint_browser_obj = self
        except Exception as e:
            st.error(f"⚠️ Browser Launch Failed: {e}")
            self.cleanup()
            self.browser = None
            self.page = None

    def navigate(self, url: str):
        if not self.page: return "ERROR: Browser not initialized."
        try:
            if not url.startswith("http"): url = f"https://www.google.com/search?q={url}"
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            img = self.page.screenshot(type="jpeg", quality=50)
            st.session_state.browser_view = img
            text = "\n".join([f.inner_text("body") for f in self.page.frames if not f.is_detached()])
            st.session_state.last_extracted_text = text
            return text[:4000]
        except Exception as e: return f"ERROR: {e}"

    def cleanup(self):
        if "osint_browser_obj" in st.session_state:
            obj = st.session_state.osint_browser_obj
            try:
                if hasattr(obj, 'page') and obj.page: obj.page.close()
                if hasattr(obj, 'context') and obj.context: obj.context.close()
                if hasattr(obj, 'browser') and obj.browser: obj.browser.close()
                if hasattr(obj, 'pw') and obj.pw: obj.pw.stop()
            except: pass
            finally:
                st.session_state.pop("osint_browser_obj", None)

# --- 3. TOOLS & AGENT ---

@tool
def browse_url(url: str):
    """Visits a URL via the visual browser."""
    if "browser_instance" in st.session_state:
        return st.session_state.browser_instance.navigate(url)
    return "ERROR: Browser offline."

@tool
def ddg_search(query: str):
    """Fallback search that works even if browser is down. Uses the latest DDGS API."""
    try:
        # Initializing without 'with' for broader version compatibility
        ddgs = DDGS()
        # Converting generator to list immediately to catch errors early
        results = list(ddgs.text(query, max_results=5))
        if not results:
            return "No public data found for this query."
        return str(results)
    except Exception as e:
        # Providing more context on the error
        if "rate limit" in str(e).lower():
            return "Search blocked by rate limit. Please try again in a few minutes or use the Browser tab."
        return f"Search Tool Error: {str(e)}"

@tool
def ocr_image_tool():
    """Trigger Gemini Vision OCR."""
    return "ST_SIGNAL_OCR_REQUESTED"

tools = [ddg_search, browse_url, ocr_image_tool]

def call_model(state):
    messages = state['messages']
    # OCR Logic
    if messages and isinstance(messages[-1], ToolMessage) and messages[-1].content == "ST_SIGNAL_OCR_REQUESTED":
        if state.get('image_data'):
            b64 = base64.b64encode(state['image_data']).decode()
            vision = HumanMessage(content=[{"type": "text", "text": "Extract OSINT clues."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}])
            res = llm.invoke([vision])
            return {"messages": [AIMessage(content=f"OCR RESULTS:\n{res.content}")]}
    
    # Standard Chain
    prompt = ChatPromptTemplate.from_messages([("system", "You are an Elite OSINT Agent."), MessagesPlaceholder(variable_name="messages")])
    chain = prompt | llm.bind_tools(tools)
    return {"messages": [chain.invoke({"messages": messages[-6:]})]}

workflow = StateGraph(dict)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(tools))
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", lambda x: "tools" if x["messages"][-1].tool_calls else END)
workflow.add_edge("tools", "agent")
graph = workflow.compile()

# --- 4. STREAMLIT UI ---

st.set_page_config(page_title="OSINT Agent Pro", layout="wide")

with st.sidebar:
    st.title("Settings")
    key = st.text_input("Gemini API Key", type="password")
    proxy = st.text_input("Proxy Server (Optional)")
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 Reset Browser"):
            if "browser_instance" in st.session_state:
                st.session_state.browser_instance.cleanup()
            st.session_state.pop("browser_instance", None)
            st.session_state.pop("browser_view", None)
            st.session_state.browser_instance = OSINTBrowser(proxy=proxy)
            st.rerun()
    with col2:
        if st.button("🧼 Clear Setup"):
            st.session_state.pop("pw_ready", None)
            st.info("Environment flag cleared.")

# Try to init browser if not present
if "browser_instance" not in st.session_state:
    st.session_state.browser_instance = OSINTBrowser()

t_chat, t_browser = st.tabs(["💬 Investigator", "🌐 Browser Control"])

with t_browser:
    st.subheader("Live Browser Session")
    
    browser_ready = False
    if "browser_instance" in st.session_state:
        if st.session_state.browser_instance.page is not None:
            browser_ready = True
            
    if browser_ready:
        if "browser_view" in st.session_state:
            st.image(st.session_state.browser_view, use_column_width=True)
        else:
            st.info("🌐 Browser is ready! Enter a URL below.")
            
        url = st.text_input("Navigate to URL / Search Query:")
        if st.button("Go"):
            with st.spinner("Navigating..."):
                st.session_state.browser_instance.navigate(url)
                st.rerun()
    else:
        st.error("🛑 Browser is offline.")
        st.info("Click 'Reset Browser' in the sidebar to try again.")

with t_chat:
    st.title("🔍 Autonomous OSINT")
    if not key:
        st.info("💡 Run in **Manual Mode** (enter key for AI).")
        q = st.text_input("Manual Search Query:")
        if st.button("Search"):
            results = ddg_search.invoke({"query": q})
            st.write(results)
    else:
        os.environ["GOOGLE_API_KEY"] = key
        llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro")
        if "messages" not in st.session_state: st.session_state.messages = []
        for m in st.session_state.messages:
            role = "user" if isinstance(m, HumanMessage) else "assistant"
            with st.chat_message(role): st.write(m.content)
        
        if prompt := st.chat_input("Ask agent to investigate..."):
            st.session_state.messages.append(HumanMessage(content=prompt))
            with st.chat_message("user"): st.write(prompt)
            with st.chat_message("assistant"):
                st_state = {"messages": st.session_state.messages, "image_data": None}
                resp = "Error processing."
                for out in graph.stream(st_state):
                    for node, data in out.items():
                        if node == "agent": resp = data["messages"][-1].content
                st.markdown(resp)
                st.session_state.messages.append(AIMessage(content=resp))
