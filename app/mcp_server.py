import json
import sys
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("Invoicer-MCP-Server")

# Mock Purchase Order database
MOCK_PO_DB = {
    "PO-1001": {
        "po_number": "PO-1001",
        "vendor": "Acme Corp",
        "items": [
            {"item": "Laptop", "quantity": 5, "price": 1000.0}
        ],
        "total": 5000.0,
        "status": "APPROVED"
    },
    "PO-1002": {
        "po_number": "PO-1002",
        "vendor": "Globex Corp",
        "items": [
            {"item": "Office Chair", "quantity": 10, "price": 150.0}
        ],
        "total": 1500.0,
        "status": "APPROVED"
    },
    "PO-1003": {
        "po_number": "PO-1003",
        "vendor": "Initech",
        "items": [
            {"item": "Red Stapler", "quantity": 1, "price": 25.0}
        ],
        "total": 25.0,
        "status": "APPROVED"
    },
    "PO-1004": {
        "po_number": "PO-1004",
        "vendor": "Wayne Enterprises",
        "items": [
            {"item": "Batarang", "quantity": 100, "price": 50.0}
        ],
        "total": 5000.0,
        "status": "OPEN"
    }
}

@mcp.tool()
def get_purchase_order(po_number: str) -> str:
    """Retrieve details for a specific Purchase Order (PO) by its PO number.

    Args:
        po_number: The unique PO number (e.g., PO-1001).

    Returns:
        A JSON string containing PO details or an error message.
    """
    print(f"Retrieving PO {po_number}...", file=sys.stderr)
    po = MOCK_PO_DB.get(po_number.strip().upper())
    if po:
        return json.dumps(po)
    return json.dumps({"error": f"Purchase Order {po_number} not found."})

@mcp.tool()
def approve_payment(invoice_number: str, po_number: str, amount: float) -> str:
    """Approve payment for a matched invoice and purchase order.

    Args:
        invoice_number: The invoice identifier.
        po_number: The matched purchase order identifier.
        amount: The total amount approved for payment.

    Returns:
        A success message JSON string.
    """
    print(f"Approving payment for invoice {invoice_number} against PO {po_number} for ${amount}...", file=sys.stderr)
    return json.dumps({
        "status": "success",
        "message": f"Payment of ${amount:.2f} for Invoice {invoice_number} matched with PO {po_number} has been approved.",
        "transaction_id": f"TXN-{invoice_number.upper()}-{po_number.upper()}"
    })

@mcp.tool()
def flag_discrepancy(invoice_number: str, po_number: str, details: str) -> str:
    """Flag a discrepancy between an invoice and a purchase order for review.

    Args:
        invoice_number: The invoice identifier.
        po_number: The purchase order identifier.
        details: Explanation of the discrepancy (e.g., pricing difference, quantity mismatch).

    Returns:
        A confirmation JSON string.
    """
    print(f"Flagging discrepancy for invoice {invoice_number}: {details}...", file=sys.stderr)
    return json.dumps({
        "status": "flagged",
        "message": f"Invoice {invoice_number} has been flagged for audit review due to PO {po_number} discrepancies.",
        "details": details
    })

@mcp.tool()
def send_vendor_email(vendor_name: str, subject: str, body: str) -> str:
    """Send an email notification to the vendor regarding invoice matching or discrepancy resolution.

    Args:
        vendor_name: The name of the vendor.
        subject: The subject of the email.
        body: The email content/message body.

    Returns:
        A confirmation JSON string.
    """
    print(f"Sending email to vendor {vendor_name} with subject: {subject}...", file=sys.stderr)
    return json.dumps({
        "status": "sent",
        "vendor": vendor_name,
        "subject": subject,
        "message": "Email simulation completed successfully."
    })

if __name__ == "__main__":
    mcp.run()
