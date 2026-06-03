import streamlit as st
import os
import io
import time
import base64
import re
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

# --- 1. TACTICAL OSINT UTILS (Traditional but Advanced) ---

def extract_intel_from_text(text: str):
    """Uses RegEx to find OSINT clues in raw page text."""
    intel = {
        "Emails": list(set(re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text))),
        "Social Media": list(set(re.findall(r'(?:twitter\.com|linkedin\.com|instagram\.com|facebook\.com|github\.com)/[a-zA-Z0-9._-]+', text))),
        "Phones": list(set(re.findall(r'\+?\d{1,4}?[-.\s]?\(?\d{1,3}?\)?[-.\s]?\d{1,4}[-.\s]?\d{1,4}[-.\s]?\d{1,9}', text)))
    }
    return intel

# --- 2. ENHANCED INFRASTRUCTURE ---

class OSINTBrowser:
    def __init__(self, proxy=None):
        self.cleanup()
        try:
            import subprocess
            subprocess.run(["playwright", "install", "chromium"], check=True)
        except: pass

        self.pw = sync_playwright().start()
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        browser_config = {"headless": True, "args": launch_args}
        if proxy: browser_config["proxy"] = {"server": proxy}

        self.browser = self.pw.chromium.launch(**browser_config)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        self.page = self.context.new_page()
        if STEALTH_AVAILABLE: stealth_sync(self.page)
        st.session_state.osint_browser_obj = self

    def navigate(self, url: str):
        try:
            if not url.startswith("http"): url = f"https://www.google.com/search?q={url}"
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return self.capture_state()
        except Exception as e:
            return f"ERROR: {str(e)}"

    def capture_state(self):
        st.session_state.browser_view = self.page.screenshot(type="jpeg", quality=60)
        all_text = [f.inner_text("body") for f in self.page.frames if f.is_detached() is False]
        content = "\n".join(all_text)
        st.session_state.last_extracted_text = content
        return content[:5000]

    def manual_action(self, action_type: str, selector: str = "", text: str = ""):
        try:
            if action_type == "click": self.page.click(selector, timeout=5000)
            elif action_type == "type": self.page.fill(selector, text, timeout=5000)
            elif action_type == "enter": self.page.keyboard.press("Enter")
            return self.capture_state()
        except Exception as e: return f"Action Failed: {str(e)}"

    def cleanup(self):
        if "osint_browser_obj" in st.session_state:
            try:
                st.session_state.osint_browser_obj.browser.close()
                st.session_state.osint_browser_obj.pw.stop()
            except: pass

# --- 3. AGENT TOOLS ---

@tool
def browse_url(url: str):
    """Visits a URL via visual browser."""
    return st.session_state.browser_instance.navigate(url)

@tool
def ddg_search(query: str):
    """Public search."""
    try:
        with DDGS() as ddgs: return str([r for r in ddgs.text(query, max_results=5)])
    except: return "Search blocked."

@tool
def ocr_image_tool():
    """Gemini Vision OCR."""
    return "ST_SIGNAL_OCR_REQUESTED"

# --- 4. INTERFACE SETUP ---

st.set_page_config(page_title="Autonomous OSINT Pro", layout="wide")

if "browser_instance" not in st.session_state:
    st.session_state.browser_instance = OSINTBrowser()

with st.sidebar:
    st.title("OSINT Settings")
    key = st.text_input("Gemini API Key (Optional)", type="password")
    proxy = st.text_input("Proxy Server", placeholder="http://host:port")
    
    st.divider()
    st.subheader("Target Credentials")
    u_name = st.text_input("Username")
    p_word = st.text_input("Password", type="password")
    
    if st.button("Reset Session"):
        st.session_state.browser_instance.cleanup()
        del st.session_state.browser_instance
        st.rerun()

# --- 5. EXECUTION MODES ---

if key:
    # --- AI AGENT MODE ---
    os.environ["GOOGLE_API_KEY"] = key
    llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro")
    
    # [Agent Graph Logic remains as before...]
    def call_model(state):
        messages = state['messages']
        if messages and isinstance(messages[-1], ToolMessage) and messages[-1].content == "ST_SIGNAL_OCR_REQUESTED":
            if state['image_data']:
                img_b64 = base64.b64encode(state['image_data']).decode()
                vision_msg = HumanMessage(content=[{"type": "text", "text": "Extract OSINT clues."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}])
                ocr_result = llm.invoke([vision_msg])
                return {"messages": [AIMessage(content=f"OCR RESULTS:\n{ocr_result.content}")]}
        prompt = ChatPromptTemplate.from_messages([("system", "You are an Elite OSINT Agent."), MessagesPlaceholder(variable_name="messages")])
        chain = prompt | llm.bind_tools([ddg_search, browse_url, ocr_image_tool])
        return {"messages": [chain.invoke({"messages": messages[-6:]})]}

    workflow = StateGraph(dict)
    workflow.add_node("agent", call_model)
    workflow.add_node("tools", ToolNode([ddg_search, browse_url, ocr_image_tool]))
    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", lambda x: "tools" if x["messages"][-1].tool_calls else END)
    workflow.add_edge("tools", "agent")
    app = workflow.compile()

    # [Standard Chat UI...]
    tab_chat, tab_browser = st.tabs(["🤖 AI Investigator", "🌐 Visual Browser"])
else:
    # --- TACTICAL MANUAL MODE ---
    st.info("💡 Running in **Manual Tactical Mode** (No API Key).")
    tab_manual, tab_browser = st.tabs(["⚡ Tactical Dashboard", "🌐 Visual Browser"])
    
    with tab_manual:
        st.subheader("Manual OSINT Automation")
        target_query = st.text_input("Enter Target Name / Username / URL")
        
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("🔍 Multi-Engine Search"):
                st.session_state.browser_instance.navigate(f"https://www.google.com/search?q={target_query}")
                st.success("Google Search Triggered.")
        with c2:
            if st.button("📧 Extract Page Intel"):
                if "last_extracted_text" in st.session_state:
                    intel = extract_intel_from_text(st.session_state.last_extracted_text)
                    st.json(intel)
                else: st.warning("Visit a page first!")
        with c3:
            if st.button("🔗 Find All Links"):
                if "last_extracted_text" in st.session_state:
                    links = re.findall(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', st.session_state.last_extracted_text)
                    st.write(list(set(links))[:50])

with tab_browser:
    st.subheader("Visual Browser Control")
    if "browser_view" in st.session_state:
        st.image(st.session_state.browser_view, use_column_width=True)
        col_ctrl, col_click = st.columns([2, 1])
        with col_ctrl:
            nav_url = st.text_input("Navigate to:")
            if st.button("Go"): 
                st.session_state.browser_instance.navigate(nav_url)
                st.rerun()
        with col_click:
            st.write("Precision Controls")
            sel = st.text_input("CSS Selector")
            if st.button("Click"): 
                st.session_state.browser_instance.manual_action("click", sel)
                st.rerun()
    else:
        st.info("Enter a URL to start the visual browser.")

if key and 'tab_chat' in locals():
    with tab_chat:
        st.title("🔍 Autonomous Agent")
        if "messages" not in st.session_state: st.session_state.messages = []
        for msg in st.session_state.messages:
            role = "user" if isinstance(msg, HumanMessage) else "assistant"
            with st.chat_message(role): st.write(msg.content)
        
        if prompt := st.chat_input("Ask the agent to investigate..."):
            st.session_state.messages.append(HumanMessage(content=prompt))
            with st.chat_message("user"): st.write(prompt)
            with st.chat_message("assistant"):
                state = {"messages": st.session_state.messages, "image_data": None}
                for output in app.stream(state):
                    for node, data in output.items():
                        if node == "agent": response = data["messages"][-1].content
                st.markdown(response)
                st.session_state.messages.append(AIMessage(content=response))
