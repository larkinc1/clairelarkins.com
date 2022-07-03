"""
Hacky strategy for downloading static HTML data from a Notion page to be
served by GitHub pages. Takes a little surgery but it's free!
"""
from datetime import datetime
import json
import requests
import os
from pathlib import Path
import shutil
import tempfile
from time import sleep
import urllib.parse
import uuid
import zipfile


class NotionClient:
    """
    Basic client for sending requests to Notion.
    """

    NOTION_API_ROOT = "https://www.notion.so/api/v3"

    def __init__(self, token: str):
        """
        The token comes from a 2fa login.
        """
        self.token = token

    @classmethod
    def ask_otp(cls, email: str) -> dict:
        """
        Generate a login code from email.
        """
        response = requests.request(
            "POST",
            f"{cls.NOTION_API_ROOT}/sendTemporaryPassword",
            json={
                "email": email,
                "disableLoginLink": False,
                "native": False,
                "isSignup": False,
            },
        )
        response.raise_for_status()
        json_response = response.json()
        return {
            "csrf_state": json_response["csrfState"],
            "csrf_cookie": response.cookies["csrf"],
        }

    @classmethod
    def get_token(cls, csrf_values, otp) -> str:
        """
        Get a token from an email login.
        """
        response = requests.request(
            "POST",
            f"{cls.NOTION_API_ROOT}/loginWithEmail",
            json={"state": csrf_values["csrf_state"], "password": otp},
            cookies={"csrf": csrf_values["csrf_cookie"]},
        )
        response.raise_for_status()
        return response.cookies["token_v2"]

    def _send_post_request(self, path, body):
        response = requests.request(
            "POST",
            f"{self.NOTION_API_ROOT}/{path}",
            json=body,
            cookies={"token_v2": self.token},
        )
        print(response.request.body)
        response.raise_for_status()
        return response.json()

    def launch_export_block_task(self, space_id: str, block_id: str):
        """
        Export a given block (page).
        """
        return self._send_post_request(
            "enqueueTask",
            {
                "task": {
                    "eventName": "exportBlock",
                    "request": {
                        "block": {"id": block_id, "spaceId": space_id},
                        "recursive": False,
                        "exportOptions": {
                            "exportType": "html",
                            "timeZone": "America/New_York",
                            "locale": "en",
                        },
                    },
                }
            },
        )["taskId"]

    def get_user_task_status(self, task_id: str) -> dict:
        """
        Get the status of export tasks.
        """
        task_statuses = self._send_post_request("getTasks", {"taskIds": [task_id]})["results"]
        print(task_statuses)
        return list(filter(lambda task_status: task_status["id"] == task_id, task_statuses))[0]

    def download_page(self, space_id: str, block_id: str) -> Path:
        """
        Download the given page as a zip file.
        """
        task_id = self.launch_export_block_task(space_id=space_id, block_id=block_id)

        wait_time = 3  # [s]
        while True:
            task_status = self.get_user_task_status(task_id)
            if "status" in task_status and task_status["status"]["type"] == "complete":
                break
            print(f"...Export still in progress, waiting for {wait_time} seconds")
            sleep(wait_time)
        print("Export task is finished")
        print(task_status)

        export_link = task_status["status"]["exportURL"]
        print(f"Downloading zip export from {export_link}")

        tmp_dir = (
            Path(tempfile.gettempdir())
            / f'export_{block_id}_{datetime.now().strftime("%Y-%m-%d-%H-%M-%S")}'
        )
        tmp_dir.mkdir()

        export_file_name = tmp_dir / "output.zip"
        print(f"Downloading to: {export_file_name}")

        block_size = 1024
        with requests.get(export_link, stream=True, allow_redirects=True) as response:
            with open(export_file_name, "wb") as export_file_handle:
                for data in response.iter_content(block_size):
                    export_file_handle.write(data)

        zip_extract_dir = tmp_dir / "zip_extract"
        with zipfile.ZipFile(export_file_name, "r") as f:
            f.extractall(zip_extract_dir)

        return zip_extract_dir


def rewrite_html(output_dir: Path) -> None:
    """
    Rewrite exported html and assets to not contain the export
    UUID and instead be a generic index.html and an assets folder.
    """
    matches = list(output_dir.glob("*.html"))
    assert len(matches) == 1
    html_path = matches[0]
    name = html_path.name[:-5]

    html_data = html_path.read_text()

    # Replace asset paths
    url_encoded_name = urllib.parse.quote(name)
    html_data = html_data.replace(url_encoded_name, "assets")

    # Add custom stylesheet
    html_data += '\n<link rel="stylesheet" href="styles.css" />'

    # Add favicon data
    # TODO: Probably kill?
    html_data += """\n\n
    <link rel="apple-touch-icon" sizes="180x180" href="/favicon/apple-touch-icon.png">
    <link rel="icon" type="image/png" sizes="32x32" href="/favicon/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/favicon/favicon-16x16.png">
    <link rel="manifest" href="/favicon/site.webmanifest">
    <link rel="mask-icon" href="/favicon/safari-pinned-tab.svg" color="#5bbad5">
    <link rel="shortcut icon" href="/favicon/favicon.ico">
    <meta name="msapplication-TileColor" content="#da532c">
    <meta name="msapplication-config" content="/favicon/browserconfig.xml">
    <meta name="theme-color" content="#ffffff">
    """

    html_path.write_text(html_data)

    html_path.rename(output_dir / "index.html")
    (html_path.parent / name).rename(html_path.parent / "assets")


def load_notion_config() -> dict:
    """
    Load a notion config file.
    """
    config_file = Path(__file__).parent / "notion_config.json"
    data = json.loads(config_file.read_text())

    # Convert to canonical UUID format
    data["space_id"] = str(uuid.UUID(data["space_id"]))
    data["block_id"] = str(uuid.UUID(data["block_id"]))

    return data


def main():
    """
    Download a notion page as a website into this repo.
    """
    config = load_notion_config()

    token = os.getenv("NOTION_TOKEN")
    if not token:
        csrf_values = NotionClient.ask_otp(email=config["email"])
        print(f"A one temporary password has been sent to your email address {config['email']}")
        otp = input("Temporary password: ")
        token = NotionClient.get_token(csrf_values, otp)
        print(f'Save this token as NOTION_TOKEN and re-run:\nexport NOTION_TOKEN="{token}"')
        return

    client = NotionClient(token=token)

    output_dir = client.download_page(
        space_id=config["space_id"],
        block_id=config["block_id"],
    )

    rewrite_html(output_dir)

    shutil.copytree(src=output_dir, dst=Path(__file__).parent, dirs_exist_ok=True)


if __name__ == "__main__":
    main()
