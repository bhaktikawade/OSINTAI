import streamlit as st
import os
import io
import requests
from typing import Annotated, List, TypedDict, Union
from PIL import Image
import easyocr
import numpy as np
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

# --- 1. TOOL DEFINITIONS ---

def extract_image_text(image_bytes: bytes):
    """Extracts text from an image using OCR."""
    try:
        reader = get_ocr_reader()
        img = Image.open(io.BytesIO(image_bytes))
        img_np = np.array(img)
        result = reader.readtext(img_np, detail=0)
        text = " ".join(result).strip()
        
        if not text:
            return "ERROR: No legible text found in image. Please provide a clearer image."
        return f"Extracted Text: {text}"
    except Exception as e:
        return f"ERROR: OCR failed: {str(e)}"

@tool
def ddg_search(query: str):
    """Search DuckDuckGo for names, usernames, or clues. Returns search results."""
    with DDGS() as ddgs:
        results = [r for r in ddgs.text(query, max_results=5)]
        return str(results)

@tool
def scrape_website(url: str):
    """Scrapes a URL to find specific intelligence from paragraph tags."""
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = soup.find_all('p')
        text = "\n".join([p.get_text() for p in paragraphs[:10]])
        return text if text else "No content found in <p> tags."
    except Exception as e:
        return f"ERROR: Failed to scrape {url}: {str(e)}"

# Define a custom tool for OCR that handles the state injection
@tool
def extract_image_text_tool():
    """Extracts text from the currently uploaded image. No arguments needed."""
    # This will be handled in the tool node override
    return "This is a placeholder"

# Singleton reader to avoid reloading models
@st.cache_resource
def get_ocr_reader():
    return easyocr.Reader(['en'])

tools = [ddg_search, scrape_website, extract_image_text_tool]

# Custom ToolNode to handle the OCR logic specifically
class OSINTToolNode(ToolNode):
    def __init__(self, tools, image_data_provider):
        super().__init__(tools)
        self.image_data_provider = image_data_provider

    def call_tool(self, tool_call, state):
        if tool_call["name"] == "extract_image_text_tool":
            image_data = state.get("image_data")
            if not image_data:
                return ToolMessage(
                    tool_call_id=tool_call["id"],
                    content="ERROR: No image was uploaded. Please upload an image in the sidebar."
                )
            result = extract_image_text(image_data)
            return ToolMessage(tool_call_id=tool_call["id"], content=result)
        return super().call_tool(tool_call, state)

tool_node = OSINTToolNode(tools, image_data_provider=lambda: st.session_state.get("uploaded_image"))

# --- 2. LANGGRAPH AGENT LOGIC ---

class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], "The messages in the conversation"]
    image_data: Union[bytes, None]

def call_model(state: AgentState):
    messages = state['messages']
    
    # System prompt to enforce investigative behavior and guardrails
    prompt = ChatPromptTemplate.from_messages([
        ("system", (
            "You are an Elite OSINT Autonomous Agent. Your goal is to gather actionable intelligence "
            "based on user input (text, usernames, or images).\n\n"
            "WORKFLOW GUIDELINES:\n"
            "1. If an image is provided in the context, your FIRST action MUST be to use 'extract_image_text_tool'.\n"
            "2. Evaluate OCR output: If the result starts with 'ERROR' or is clearly garbage/nonsense, "
            "STOP and tell the user the image is not clear enough.\n"
            "3. If OCR is successful, use the extracted text to search for more info via 'ddg_search'.\n"
            "4. If search results provide URLs, use 'scrape_website' to gather deep intelligence.\n"
            "5. Iterate until you have a comprehensive report.\n"
            "6. FINAL OUTPUT: Format your findings in a clean Markdown report with sections for 'Key Findings', 'Sources', and 'Actionable Intelligence'."
        )),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm.bind_tools(tools)
    response = chain.invoke({"messages": messages})
    return {"messages": [response]}

def should_continue(state: AgentState):
    messages = state['messages']
    last_message = messages[-1]
    
    # If the LLM didn't call a tool, we finish
    if not last_message.tool_calls:
        return END
    
    # Check for the OCR error guardrail in the last tool message
    # If the previous step was OCR and it failed, we might want to branch to END
    # However, ReAct usually handles this by the LLM seeing the error message.
    return "tools"

# Construct the graph
workflow = StateGraph(AgentState)

workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)

workflow.set_entry_point("agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

app = workflow.compile()

# --- 3. STREAMLIT UI ---

st.set_page_config(page_title="Autonomous OSINT Agent", layout="wide")
st.title("🔍 Autonomous OSINT Investigative Agent")

# Sidebar Configuration
with st.sidebar:
    st.header("Settings")
    api_key = st.text_input("Google API Key", type="password")
    if api_key:
        os.environ["GOOGLE_API_KEY"] = api_key
    
    st.divider()
    st.header("Upload Intelligence")
    uploaded_file = st.file_uploader("Upload an image for OCR analysis", type=["jpg", "jpeg", "png"])
    
    if st.button("Clear History"):
        st.session_state.messages = []
        st.rerun()

# Initialize LLM and Session State
if "messages" not in st.session_state:
    st.session_state.messages = []

if not api_key:
    st.warning("Please enter your Google API Key in the sidebar to start.")
    st.stop()

llm = ChatGoogleGenerativeAI(model="gemini-1.5-pro")

# Display Chat History
for msg in st.session_state.messages:
    if isinstance(msg, HumanMessage):
        st.chat_message("user").write(msg.content)
    elif isinstance(msg, AIMessage) and msg.content:
        st.chat_message("assistant").write(msg.content)

# Handle Input
if prompt := st.chat_input("Enter a name, username, or investigative query..."):
    st.chat_message("user").write(prompt)
    
    # Prepare Initial State
    image_bytes = None
    initial_content = prompt
    
    if uploaded_file:
        image_bytes = uploaded_file.read()
        initial_content += f"\n[Image attached: {uploaded_file.name}]"
        # We pass the bytes via the state, but we also tell the agent it's there
        # Since extract_image_text tool needs bytes, we'll handle that in the tool call
    
    st.session_state.messages.append(HumanMessage(content=initial_content))
    
    # Run the Agent with Visual Workflow
    with st.chat_message("assistant"):
        with st.status("Agent thinking...", expanded=True) as status:
            current_state = {
                "messages": st.session_state.messages,
                "image_data": image_bytes
            }
            
            # Manual loop to update Streamlit UI during graph execution
            # Note: For production, a custom CallbackHandler passed to the LLM is better,
            # but for LangGraph, we iterate through the stream.
            
            final_response = ""
            
            for output in app.stream(current_state):
                for node_name, node_state in output.items():
                    if node_name == "agent":
                        last_msg = node_state["messages"][-1]
                        if last_msg.tool_calls:
                            for tc in last_msg.tool_calls:
                                status.write(f"🛠️ **Decided to use:** {tc['name']}")
                        if last_msg.content:
                            final_response = last_msg.content
                    
                    elif node_name == "tools":
                        last_msg = node_state["messages"][-1]
                        status.write(f"✅ **Action Output:** {last_msg.content[:200]}...")
            
            status.update(label="Investigation Complete!", state="complete", expanded=False)
        
        # Display Final Markdown Report
        st.markdown(final_response)
        st.session_state.messages.append(AIMessage(content=final_response))
