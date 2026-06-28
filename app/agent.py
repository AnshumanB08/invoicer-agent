import os
import re
import sys
import json
import datetime
from pydantic import BaseModel, Field
from google.genai import types
from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.workflow import Workflow, START, node
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from .config import config

# Set Gemini API key usage environment variables
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

# --- MCP Toolset Initialization ---
# Construct absolute path to mcp_server.py dynamically
current_dir = os.path.dirname(os.path.abspath(__file__))
mcp_server_path = os.path.join(current_dir, "mcp_server.py")

# Create standard stdio MCP Server parameters
stdio_params = StdioServerParameters(
    command=sys.executable,
    args=[mcp_server_path]
)

# Initialize MCP Toolset (will start/stop mcp_server.py as needed)
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(server_params=stdio_params)
)

# --- Pydantic Schemas for Structured Node Outputs ---

class InvoiceItem(BaseModel):
    item: str = Field(description="The name of the item.")
    quantity: int = Field(description="The quantity of the item.")
    price: float = Field(description="The unit price of the item.")

class InvoiceData(BaseModel):
    invoice_number: str = Field(description="The invoice number extracted from the document.")
    vendor_name: str = Field(description="The vendor name.")
    po_number: str = Field(description="The referenced Purchase Order number (e.g. PO-1001).")
    amount: float = Field(description="The total invoice amount.")
    items: list[InvoiceItem] = Field(description="List of line items on the invoice.")

class AuditResult(BaseModel):
    invoice_number: str = Field(description="The invoice number.")
    po_number: str = Field(description="The Purchase Order number.")
    vendor_name: str = Field(description="The vendor name.")
    amount: float = Field(description="The total invoice amount.")
    is_matched: bool = Field(description="True if invoice details match PO details, False otherwise.")
    discrepancy_reason: str = Field(description="Explanation of any discrepancy found (e.g., price mismatch, missing items).")
    requires_approval: bool = Field(description="True if human approval is needed due to discrepancy or total amount exceeding limit.")

class PaymentResult(BaseModel):
    status: str = Field(description="The status of the transaction (e.g., success, flagged, sent).")
    message: str = Field(description="Confirmation message explaining the outcome.")
    transaction_id: str = Field(default="", description="The transaction ID if successful, empty otherwise.")

class EmailResult(BaseModel):
    status: str = Field(description="The status of the email (e.g., sent).")
    vendor: str = Field(description="The name of the vendor notified.")
    subject: str = Field(description="The subject line of the sent email.")
    message: str = Field(description="Outcome message.")

# --- Workflow Node Implementations ---

def security_checkpoint(ctx: Context, node_input: types.Content) -> Event:
    """Workflow function node to perform security validation and PII scrubbing.

    Args:
        ctx: The workflow context.
        node_input: The raw user input content.

    Returns:
        Event routing the flow to either 'clean' (clean) or 'security_alert' (blocked).
    """
    text_content = ""
    if hasattr(node_input, 'parts') and node_input.parts:
        text_content = "".join([p.text for p in node_input.parts if p.text])
    elif isinstance(node_input, str):
        text_content = node_input

    # 1. Prompt Injection check
    injection_keywords = ["ignore previous instructions", "system prompt", "override role", "developer mode", "do anything now"]
    detected_injection = False
    for kw in injection_keywords:
        if kw in text_content.lower():
            detected_injection = True
            break

    if detected_injection:
        audit_log = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": "security_violation",
            "severity": "CRITICAL",
            "reason": "Prompt injection attempt detected.",
            "input_preview": text_content[:100]
        }
        print(json.dumps(audit_log), file=sys.stderr)
        return Event(
            output="Request blocked due to security validation failure (prompt injection).",
            route="security_alert"
        )

    # 2. PII Scrubbing (Scrub SSN or credit cards from input if any)
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    cc_pattern = r'\b(?:\d[ -]*?){13,16}\b'
    scrubbed_text = re.sub(ssn_pattern, "[SSN_REDACTED]", text_content)
    scrubbed_text = re.sub(cc_pattern, "[CARD_REDACTED]", scrubbed_text)

    # 3. Domain-Specific Rule: Flag empty/blank text
    if not scrubbed_text.strip():
        audit_log = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": "security_violation",
            "severity": "WARNING",
            "reason": "Empty invoice input received.",
            "input_preview": ""
        }
        print(json.dumps(audit_log), file=sys.stderr)
        return Event(
            output="Request blocked: Input text is empty.",
            route="security_alert"
        )

    # Audit log for clean pass
    audit_log = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "security_check_passed",
        "severity": "INFO",
        "message": "Input passed security validation."
    }
    print(json.dumps(audit_log), file=sys.stderr)

    ctx.state["scrubbed_input"] = scrubbed_text
    return Event(output=scrubbed_text, route="clean")

def security_event_handler(ctx: Context, node_input: str) -> Event:
    """Terminal node for blocked security inputs."""
    message = f"🔒 Security Block: {node_input}"
    return Event(
        output={"status": "blocked", "message": message},
        content=types.Content(role="model", parts=[types.Part.from_text(text=message)])
    )

# --- Sub-Agents ---

# Sub-Agent 1: Extracts structured invoice details
invoice_parser = LlmAgent(
    name="invoice_parser",
    model=Gemini(model=config.model),
    instruction=(
        "You are an Invoice Extraction Specialist. Your task is to extract structured details from the provided invoice text.\n"
        "Identify:\n"
        "- The invoice number\n"
        "- The vendor name\n"
        "- The referenced Purchase Order (PO) number (e.g. PO-1001)\n"
        "- The total invoice amount\n"
        "- The itemized lines (each with item name, quantity, unit price)\n"
        "Ensure the output conforms strictly to the schema."
    ),
    output_schema=InvoiceData,
    output_key="invoice_data"
)

# Sub-Agent 2: Audits invoice details against PO from MCP Server
invoice_auditor = LlmAgent(
    name="invoice_auditor",
    model=Gemini(model=config.model),
    instruction=(
        "You are an Invoice Auditor. Your task is to match the invoice details against the Purchase Order (PO) stored in the database.\n"
        "1. Retrieve the Purchase Order details by calling the `get_purchase_order` tool using the PO number extracted from the invoice.\n"
        "2. Compare the vendor, total amount, and items between the invoice and the PO.\n"
        "3. Determine if they match: `is_matched` must be True ONLY if vendor, total amount, and items align. Otherwise, set it to False.\n"
        "4. Set `requires_approval` to True if there is any discrepancy, OR if the total invoice amount is greater than 5000.0 (high-value threshold).\n"
        "5. Explain any discrepancies in the `discrepancy_reason` field. If there are none, write 'None'."
    ),
    tools=[mcp_toolset],
    output_schema=AuditResult,
    output_key="audit_result"
)

# --- Audit Router Function Node ---

def audit_router(ctx: Context, node_input: dict) -> Event:
    """Routes the workflow based on the audit result."""
    ctx.state["audit_result"] = node_input
    
    # Domain-specific logging and routing
    requires_approval = node_input.get("requires_approval", False)
    is_matched = node_input.get("is_matched", False)

    if requires_approval or not is_matched:
        severity = "WARNING"
        route = "NEEDS_REVIEW"
    else:
        severity = "INFO"
        route = "MATCHED"

    audit_log = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "audit_complete",
        "severity": severity,
        "invoice_number": node_input.get("invoice_number"),
        "po_number": node_input.get("po_number"),
        "is_matched": is_matched,
        "requires_approval": requires_approval,
        "route_selected": route
    }
    print(json.dumps(audit_log), file=sys.stderr)
    
    return Event(output=node_input, route=route)

# --- Human-in-the-Loop Node ---

@node(rerun_on_resume=True)
async def human_reviewer(ctx: Context, node_input: dict):
    """Pauses the workflow for human approval/review of flagged invoices."""
    if not ctx.resume_inputs:
        reason = node_input.get("discrepancy_reason", "No discrepancy explained.")
        amount = node_input.get("amount", 0.0)
        message = (
            f"✋ **Human Review Required** for Invoice {node_input.get('invoice_number')}.\n"
            f"Vendor: {node_input.get('vendor_name')}\n"
            f"Amount: ${amount:.2f}\n"
            f"PO Reference: {node_input.get('po_number')}\n"
            f"Reason for review: {reason}\n\n"
            f"Please respond with 'APPROVED' or 'REJECTED'."
        )
        yield RequestInput(
            interrupt_id="approval",
            message=message
        )
        return

    # Process human input response
    response_text = ctx.resume_inputs.get("approval", "").strip().upper()
    
    audit_log = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "human_review_resolved",
        "severity": "INFO",
        "user_decision": response_text
    }
    print(json.dumps(audit_log), file=sys.stderr)

    if "APPROVE" in response_text:
        yield Event(output={"decision": "APPROVED", "notes": response_text}, route="APPROVED")
    else:
        yield Event(output={"decision": "REJECTED", "notes": response_text}, route="REJECTED")

# --- Sub-Agent 3: Processes payment for matched/approved invoices ---
payment_agent = LlmAgent(
    name="payment_agent",
    model=Gemini(model=config.model),
    instruction=(
        "You are the Payment Agent. Your task is to approve payment for the invoice using the `approve_payment` tool.\n"
        "Extract the invoice_number, po_number, and amount from the input, call `approve_payment` tool to register it in the transaction ledger, and output the status result."
    ),
    tools=[mcp_toolset],
    output_schema=PaymentResult,
    output_key="payment_result"
)

# --- Sub-Agent 4: Notifies vendor of rejected invoices ---
vendor_notifier = LlmAgent(
    name="vendor_notifier",
    model=Gemini(model=config.model),
    instruction=(
        "You are the Vendor Notifier Agent. The invoice was rejected. Your task is to write a polite email notification to the vendor explaining the rejection details and call the `send_vendor_email` tool to notify them."
    ),
    tools=[mcp_toolset],
    output_schema=EmailResult,
    output_key="rejection_email_result"
)

# --- Final Output Formatting Node ---

def final_output(ctx: Context, node_input: dict) -> Event:
    """Formats the final summary message for display in the Web UI."""
    status = node_input.get("status", "")
    message = node_input.get("message", "Processing completed.")

    if status == "success":
        display_text = (
            f"✅ **Invoice Processing Successful!**\n\n"
            f"{message}\n\n"
            f"Transaction ID: `{node_input.get('transaction_id')}`"
        )
    elif status == "sent":
        display_text = (
            f"❌ **Invoice Rejected.**\n\n"
            f"Notification sent to vendor: **{node_input.get('vendor')}**\n"
            f"Subject: *{node_input.get('subject')}*\n\n"
            f"Details: Rejection email has been successfully queued."
        )
    else:
        display_text = f"ℹ️ **System Response:**\n\n{message}"

    return Event(
        output=node_input,
        content=types.Content(role="model", parts=[types.Part.from_text(text=display_text)])
    )

# --- Workflow Definition ---

edges = [
    ('START', security_checkpoint),
    (security_checkpoint, {"clean": invoice_parser, "security_alert": security_event_handler}),
    (invoice_parser, invoice_auditor),
    (invoice_auditor, audit_router),
    (audit_router, {"MATCHED": payment_agent, "NEEDS_REVIEW": human_reviewer}),
    (human_reviewer, {"APPROVED": payment_agent, "REJECTED": vendor_notifier}),
    (payment_agent, final_output),
    (vendor_notifier, final_output),
    (security_event_handler, final_output)
]

root_agent = Workflow(
    name="invoicer_workflow",
    edges=edges,
    description="Automated invoice parsing, auditing, discrepancy flagging, and payment approval workflow."
)

# App Container
app = App(
    root_agent=root_agent,
    name="app",
)
