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
from playwright_stealth import stealth_sync

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

# --- 1. ENHANCED INFRASTRUCTURE (Fixes #1, #5, #7) ---

class OSINTBrowser:
    def __init__(self, proxy=None):
        self.cleanup() # Ensure no zombies from previous attempts
        self.pw = sync_playwright().start()
        
        # Proxy configuration (Fix #1)
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
        browser_config = {"headless": True, "args": launch_args}
        if proxy:
            browser_config["proxy"] = {"server": proxy}

        self.browser = self.pw.chromium.launch(**browser_config)
        self.context = self.browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        )
        self.page = self.context.new_page()
        stealth_sync(self.page)
        st.session_state.osint_browser_obj = self # Store for cleanup

    def navigate(self, url: str):
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return self.capture_state()
        except Exception as e:
            return f"ERROR: Browser failed: {str(e)}"

    def capture_state(self):
        # Capture screenshot for UI
        screenshot = self.page.screenshot(type="jpeg", quality=60)
        st.session_state.browser_view = screenshot
        
        # Deep text extraction including frames (Fix #3)
        all_text = []
        for frame in self.page.frames:
            try:
                all_text.append(frame.inner_text("body"))
            except: continue
        
        content = "\n".join(all_text)
        return content[:5000] # Limit for Gemini context

    def click_coordinates(self, x_pct: float, y_pct: float):
        # Viewport-independent clicking (Fix #7)
        width = 1280
        height = 720
        abs_x = int((x_pct / 100) * width)
        abs_y = int((y_pct / 100) * height)
        self.page.mouse.click(abs_x, abs_y)
        time.sleep(1) # Wait for potential navigation
        return self.capture_state()

    def manual_action(self, action_type: str, selector: str = "", text: str = ""):
        # Deep search in all frames for selector (Fix #3)
        target = self.page
        for frame in self.page.frames:
            if frame.locator(selector).count() > 0:
                target = frame
                break
        
        try:
            if action_type == "click": target.click(selector)
            elif action_type == "type": target.fill(selector, text)
            elif action_type == "enter": self.page.keyboard.press("Enter")
            return self.capture_state()
        except Exception as e:
            return f"Action Failed: {str(e)}"

    def cleanup(self):
        # Explicit zombie killing (Fix #5)
        if "osint_browser_obj" in st.session_state:
            try:
                st.session_state.osint_browser_obj.browser.close()
                st.session_state.osint_browser_obj.pw.stop()
            except: pass

# --- 2. TOOLS (Fix #6) ---

@tool
def browse_url(url: str):
    """Visits a URL. If credentials are provided in the chat, use them to log in if needed."""
    return st.session_state.browser_instance.navigate(url)

@tool
def ddg_search(query: str):
    """Search for public data."""
    try:
        with DDGS() as ddgs:
            return str([r for r in ddgs.text(query, max_results=5)])
    except: return "Search blocked. Use manual navigation."

@tool
def ocr_image_tool():
    """Extracts text from uploaded image via Gemini Vision."""
    return "ST_SIGNAL_OCR_REQUESTED"

tools = [ddg_search, browse_url, ocr_image_tool]

# --- 3. AGENT LOGIC (Fix #4, #6) ---

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], "History"]
    image_data: Union[bytes, None]
    credentials: dict

def call_model(state: AgentState):
    messages = state['messages']
    creds = state.get("credentials", {})
    
    # Handle OCR Request
    if messages and isinstance(messages[-1], ToolMessage) and messages[-1].content == "ST_SIGNAL_OCR_REQUESTED":
        if state['image_data']:
            img_b64 = base64.b64encode(state['image_data']).decode()
            vision_msg = HumanMessage(content=[
                {"type": "text", "text": "Extract all OSINT clues from this image."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
            ])
            # Simple retry for rate limits (Fix #4)
            for _ in range(3):
                try:
                    ocr_result = llm.invoke([vision_msg])
                    return {"messages": [AIMessage(content=f"OCR RESULTS:\n{ocr_result.content}")]}
                except: time.sleep(2)
            return {"messages": [AIMessage(content="Error: Rate limited.")]}

    # System prompt handling login (Fix #6)
    cred_context = f"\nAVAILABLE CREDENTIALS: {creds}\n" if creds else "\nNO CREDENTIALS PROVIDED. Search public data only.\n"
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", f"You are an Elite OSINT Agent.{cred_context}"
                   "1. Use credentials only if provided. Otherwise, scrape public pages.\n"
                   "2. If you see a Login wall and have no creds, search for cached or alternative public versions.\n"
                   "3. If a CAPTCHA appears, stop and ask the user to solve it in the Browser Tab."),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    # Rate limit guard (Fix #4)
    time.sleep(1)
    chain = prompt | llm.bind_tools(tools)
    response = chain.invoke({"messages": messages[-6:]}) # Aggressive trimming for TPM limits
    return {"messages": [response]}

workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", ToolNode(tools))
workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", lambda x: "tools" if x["messages"][-1].tool_calls else END)
workflow.add_edge("tools", "agent")
app = workflow.compile()

# --- 4. UI (Fix #7, #1, #6) ---

st.set_page_config(page_title="OSINT Agent Pro", layout="wide")

with st.sidebar:
    st.title("Settings")
    key = st.text_input("Gemini API Key", type="password")
    proxy = st.text_input("Proxy Server (Optional)", placeholder="http://user:pass@host:port")
    
    st.divider()
    st.subheader("Login Vault (Fix #6)")
    target_site = st.text_input("Target Site (e.g. LinkedIn)")
    u_name = st.text_input("Username")
    p_word = st.text_input("Password", type="password")
    
    st.divider()
    img_file = st.file_uploader("Intelligence Image", type=["jpg", "png", "jpeg"])
    
    if st.button("Destroy Browser Session (Fix #5)"):
        if "browser_instance" in st.session_state:
            st.session_state.browser_instance.cleanup()
            del st.session_state.browser_instance
            st.rerun()

if not key:
    st.info("Please enter your API Key to initialize the Agent.")
    st.stop()

os.environ["GOOGLE_API_KEY"] = key
llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro")

if "browser_instance" not in st.session_state:
    st.session_state.browser_instance = OSINTBrowser(proxy=proxy)

t_chat, t_browser = st.tabs(["💬 Agent Chat", "🌐 Visual Browser"])

with t_browser:
    st.subheader("Live View & Coordinate Click (Fix #7)")
    if "browser_view" in st.session_state:
        # Use columns to allow coordinate input next to image
        col_img, col_ctrl = st.columns([3, 1])
        with col_img:
            st.image(st.session_state.browser_view, use_column_width=True)
            st.caption("Viewport: 1280x720")
        with col_ctrl:
            st.write("🎯 Precision Click")
            click_x = st.number_input("X %", 0.0, 100.0, 50.0)
            click_y = st.number_input("Y %", 0.0, 100.0, 50.0)
            if st.button("Click at Coordinates"):
                st.session_state.browser_instance.click_coordinates(click_x, click_y)
                st.rerun()
            
            st.divider()
            st.write("⌨️ Manual Controls (Fix #3)")
            sel = st.text_input("CSS Selector")
            txt = st.text_input("Input Text")
            if st.button("Click Selector"):
                st.session_state.browser_instance.manual_action("click", sel)
                st.rerun()
            if st.button("Type & Submit"):
                st.session_state.browser_instance.manual_action("type", sel, txt)
                st.session_state.browser_instance.manual_action("enter")
                st.rerun()
    else:
        st.info("Browser is idle.")

with t_chat:
    if "messages" not in st.session_state: st.session_state.messages = []
    
    for msg in st.session_state.messages:
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        with st.chat_message(role): st.write(msg.content)

    if prompt := st.chat_input("Start investigation..."):
        st.session_state.messages.append(HumanMessage(content=prompt))
        with st.chat_message("user"): st.write(prompt)
        
        creds = {target_site: {"user": u_name, "pass": p_word}} if u_name else {}
        img_bytes = img_file.read() if img_file else None
        
        with st.chat_message("assistant"):
            with st.status("Thinking...", expanded=False) as status:
                state = {"messages": st.session_state.messages, "image_data": img_bytes, "credentials": creds}
                response = "No response"
                for output in app.stream(state, config={"recursion_limit": 15}):
                    for node, data in output.items():
                        if node == "agent":
                            m = data["messages"][-1]
                            if m.tool_calls: status.write(f"Browser: {m.tool_calls[0]['name']}")
                            if m.content: response = m.content
                status.update(label="Complete", state="complete")
            st.markdown(response)
            st.session_state.messages.append(AIMessage(content=response))
