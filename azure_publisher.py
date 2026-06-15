import os

from dotenv import load_dotenv

load_dotenv()


def publish_report(html_path: str) -> str | None:
    """Upload the HTML report to Azure Blob Storage static website.

    Returns the public static website URL on success, or None if skipped/failed.
    Requires AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER in .env.
    """
    try:
        from azure.storage.blob import BlobServiceClient, ContentSettings
    except ImportError:
        print("  [publish] azure-storage-blob not installed. Run: pip install azure-storage-blob")
        return None

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    container = os.environ.get("AZURE_STORAGE_CONTAINER", "$web").strip()

    if not conn_str:
        print("  [publish] AZURE_STORAGE_CONNECTION_STRING not set — skipping upload.")
        return None

    try:
        client = BlobServiceClient.from_connection_string(conn_str)
        blob = client.get_blob_client(container=container, blob="index.html")

        with open(html_path, "rb") as f:
            blob.upload_blob(
                f,
                overwrite=True,
                content_settings=ContentSettings(
                    content_type="text/html; charset=utf-8",
                    cache_control="no-cache, no-store, must-revalidate",
                ),
            )

        # Derive static website URL from account name in connection string
        account_name = _extract_account_name(conn_str)
        if account_name:
            url = f"https://{account_name}.z13.web.core.windows.net/"
            print(f"  [publish] Uploaded to: {url}")
            return url
        return None

    except Exception as exc:
        print(f"  [publish] Upload failed: {exc}")
        return None


def _extract_account_name(conn_str: str) -> str | None:
    for part in conn_str.split(";"):
        if part.startswith("AccountName="):
            return part.split("=", 1)[1].strip()
    return None
