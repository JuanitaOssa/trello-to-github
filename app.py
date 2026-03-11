"""
Trello → GitHub Migrator
A Streamlit app to migrate Trello boards to GitHub Issues and Projects.
"""

from __future__ import annotations

import streamlit as st
import requests
from typing import Optional, Union, List, Dict
import time
import json
import re
import unicodedata


# =============================================================================
# HELPERS
# =============================================================================

def clean_title(title: str) -> str:
    """
    Clean a title string for use in GitHub API calls.

    - Strips leading/trailing whitespace
    - Removes zero-width and invisible unicode characters
    - Keeps standard emojis intact
    """
    if not title:
        return title

    # Strip whitespace
    title = title.strip()

    # Remove zero-width and invisible characters
    # These include: zero-width space, zero-width non-joiner, zero-width joiner,
    # left-to-right mark, right-to-left mark, and other format characters
    invisible_chars = re.compile(
        r'[\u200b\u200c\u200d\u200e\u200f'  # Zero-width chars and direction marks
        r'\u2060\u2061\u2062\u2063\u2064'    # Word joiner, invisible operators
        r'\ufeff'                            # Byte order mark
        r'\u00ad'                            # Soft hyphen
        r'\u034f'                            # Combining grapheme joiner
        r'\u061c'                            # Arabic letter mark
        r'\u115f\u1160'                      # Hangul fillers
        r'\u17b4\u17b5'                      # Khmer vowel inherent
        r'\u180b-\u180e'                     # Mongolian free variation selectors
        r'\uffa0'                            # Halfwidth Hangul filler
        r'\ufff0-\ufff8'                     # Specials
        r']'
    )
    title = invisible_chars.sub('', title)

    # Normalize unicode to NFC form (composed characters)
    title = unicodedata.normalize('NFC', title)

    # Remove any remaining control characters (but keep emojis)
    # Control chars are in categories Cc and Cf, but we want to keep some Cf for emojis
    cleaned = []
    for char in title:
        category = unicodedata.category(char)
        # Keep everything except control characters (Cc)
        # and format characters (Cf) that aren't emoji-related
        if category != 'Cc' and (category != 'Cf' or ord(char) > 0xFFFF):
            cleaned.append(char)

    return ''.join(cleaned).strip()


# =============================================================================
# TRELLO JSON PARSER
# =============================================================================

def parse_trello_json(json_data: dict) -> dict:
    """
    Parse a Trello board JSON export into the format used by the app.

    Trello exports contain the full board data including lists, cards, and labels.
    """
    board_name = json_data.get("name", "Imported Board")
    board_id = json_data.get("id", "imported")

    # Extract lists (filter out archived lists)
    lists = [
        {"id": lst["id"], "name": lst["name"]}
        for lst in json_data.get("lists", [])
        if not lst.get("closed", False)
    ]

    # Build a map of label IDs to label info
    label_map = {
        label["id"]: {"name": label.get("name", ""), "color": label.get("color", "")}
        for label in json_data.get("labels", [])
    }

    # Extract cards (filter out archived cards)
    cards = []
    for card in json_data.get("cards", []):
        if card.get("closed", False):
            continue

        # Get labels for this card
        card_labels = []
        for label_id in card.get("idLabels", []):
            if label_id in label_map:
                label_info = label_map[label_id]
                if label_info["name"]:  # Only include labels with names
                    card_labels.append({
                        "name": label_info["name"],
                        "color": label_info["color"]
                    })

        cards.append({
            "id": card["id"],
            "name": card.get("name", "Untitled"),
            "desc": card.get("desc", ""),
            "idList": card.get("idList", ""),
            "labels": card_labels,
            "url": card.get("url", ""),
        })

    # Extract unique labels
    labels = [
        {"id": label["id"], "name": label.get("name", ""), "color": label.get("color", "")}
        for label in json_data.get("labels", [])
        if label.get("name")
    ]

    # Create list map
    list_map = {lst["id"]: lst["name"] for lst in lists}

    return {
        "board_id": board_id,
        "board_name": board_name,
        "lists": lists,
        "cards": cards,
        "labels": labels,
        "list_map": list_map,
    }


# =============================================================================
# API CLIENTS
# =============================================================================

class TrelloClient:
    """Client for interacting with the Trello REST API."""

    BASE_URL = "https://api.trello.com/1"

    def __init__(self, api_key: str, token: str):
        self.api_key = api_key
        self.token = token

    def _make_request(self, endpoint: str, params: Optional[dict] = None) -> Union[dict, list]:
        """Make an authenticated request to the Trello API."""
        url = f"{self.BASE_URL}{endpoint}"
        params = params or {}
        params.update({"key": self.api_key, "token": self.token})

        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_boards(self) -> list[dict]:
        """Fetch all boards for the authenticated user."""
        return self._make_request("/members/me/boards", {"fields": "name,id,url"})

    def get_lists(self, board_id: str) -> list[dict]:
        """Fetch all lists in a board."""
        return self._make_request(f"/boards/{board_id}/lists", {"fields": "name,id"})

    def get_cards(self, board_id: str) -> list[dict]:
        """Fetch all cards in a board with their details."""
        return self._make_request(
            f"/boards/{board_id}/cards",
            {"fields": "name,desc,idList,labels,url"}
        )

    def get_labels(self, board_id: str) -> list[dict]:
        """Fetch all labels in a board."""
        return self._make_request(f"/boards/{board_id}/labels", {"fields": "name,color,id"})


class GitHubClient:
    """Client for interacting with the GitHub REST API."""

    BASE_URL = "https://api.github.com"

    # Map Trello colors to GitHub label colors (hex without #)
    COLOR_MAP = {
        "green": "0e8a16",
        "yellow": "fbca04",
        "orange": "d93f0b",
        "red": "b60205",
        "purple": "5319e7",
        "blue": "0052cc",
        "sky": "1d76db",
        "lime": "84b817",
        "pink": "e99695",
        "black": "333333",
        None: "ededed",
        "": "ededed",
    }

    def __init__(self, token: str, owner: str, is_org: bool = False):
        self.token = token
        self.owner = owner  # username or org name
        self._is_org_override = is_org  # User's explicit choice
        self._authenticated_user = None  # Cached authenticated user login
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None
    ) -> Optional[Union[dict, list]]:
        """Make an authenticated request to the GitHub API."""
        url = f"{self.BASE_URL}{endpoint}"

        response = requests.request(
            method,
            url,
            headers=self.headers,
            json=data,
            timeout=30
        )

        if response.status_code == 404:
            return None

        # For non-success status codes, try to extract error message
        if not response.ok:
            try:
                error_data = response.json()
                error_msg = error_data.get("message", response.text)
                errors = error_data.get("errors", [])
                if errors:
                    error_details = "; ".join(
                        e.get("message", str(e)) for e in errors
                    )
                    error_msg = f"{error_msg} ({error_details})"
            except Exception:
                error_msg = response.text

            raise requests.exceptions.HTTPError(
                f"GitHub API error {response.status_code}: {error_msg}",
                response=response
            )

        if response.status_code == 204:
            return {}

        return response.json()

    def get_authenticated_user(self) -> str:
        """Get the login name of the authenticated user."""
        if self._authenticated_user is None:
            result = self._make_request("GET", "/user")
            self._authenticated_user = result.get("login", "") if result else ""
        return self._authenticated_user

    def is_org(self) -> bool:
        """
        Determine if the owner is an organization.

        Returns True if:
        - User explicitly selected Organization mode, OR
        - The owner doesn't match the authenticated user's login
        """
        if self._is_org_override:
            return True

        # Auto-detect: if owner != authenticated user, treat as org
        auth_user = self.get_authenticated_user()
        return auth_user.lower() != self.owner.lower()

    def repo_exists(self, repo_name: str) -> bool:
        """Check if a repository exists."""
        result = self._make_request("GET", f"/repos/{self.owner}/{repo_name}")
        return result is not None

    def create_repo(self, repo_name: str, description: str = "") -> dict:
        """Create a new repository under user account or organization."""
        if self.is_org():
            endpoint = f"/orgs/{self.owner}/repos"
        else:
            endpoint = "/user/repos"

        return self._make_request("POST", endpoint, {
            "name": repo_name,
            "description": description,
            "private": False,
            "has_issues": True,
        })

    # =========================================================================
    # GitHub Projects v2 (GraphQL API)
    # =========================================================================

    def _graphql_request(self, query: str, variables: Optional[dict] = None) -> dict:
        """Make a GraphQL request to GitHub's API."""
        url = "https://api.github.com/graphql"

        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = requests.post(
            url,
            headers=self.headers,
            json=payload,
            timeout=30
        )

        if not response.ok:
            raise requests.exceptions.HTTPError(
                f"GitHub GraphQL error {response.status_code}: {response.text}",
                response=response
            )

        result = response.json()

        if "errors" in result:
            error_msgs = [e.get("message", str(e)) for e in result["errors"]]
            raise requests.exceptions.HTTPError(
                f"GitHub GraphQL error: {'; '.join(error_msgs)}"
            )

        return result.get("data", {})

    def get_project_v2(self, project_number: int) -> Optional[dict]:
        """
        Get a GitHub Project v2 by number.

        Returns project info including ID, title, and fields.
        """
        # Query differs based on whether owner is org or user
        if self.is_org():
            query = """
            query($owner: String!, $number: Int!) {
                organization(login: $owner) {
                    projectV2(number: $number) {
                        id
                        title
                        number
                    }
                }
            }
            """
            variables = {"owner": self.owner, "number": project_number}
            result = self._graphql_request(query, variables)
            return result.get("organization", {}).get("projectV2")
        else:
            query = """
            query($owner: String!, $number: Int!) {
                user(login: $owner) {
                    projectV2(number: $number) {
                        id
                        title
                        number
                    }
                }
            }
            """
            variables = {"owner": self.owner, "number": project_number}
            result = self._graphql_request(query, variables)
            return result.get("user", {}).get("projectV2")

    def get_project_status_field(self, project_id: str) -> Optional[dict]:
        """
        Get the Status field from a GitHub Project v2.

        Returns the field ID and all available status options.
        """
        query = """
        query($projectId: ID!) {
            node(id: $projectId) {
                ... on ProjectV2 {
                    fields(first: 50) {
                        nodes {
                            ... on ProjectV2SingleSelectField {
                                id
                                name
                                options {
                                    id
                                    name
                                    color
                                    description
                                }
                            }
                        }
                    }
                }
            }
        }
        """
        variables = {"projectId": project_id}
        result = self._graphql_request(query, variables)

        fields = result.get("node", {}).get("fields", {}).get("nodes", [])

        # Find the Status field (usually named "Status")
        for field in fields:
            if field and field.get("name") == "Status":
                return {
                    "id": field["id"],
                    "name": field["name"],
                    "options": field.get("options", [])
                }

        # If no "Status" field found, return the first single-select field
        for field in fields:
            if field and "options" in field:
                return {
                    "id": field["id"],
                    "name": field.get("name", "Unknown"),
                    "options": field.get("options", [])
                }

        return None

    def create_status_options(
        self,
        field_id: str,
        existing_options: list[dict],
        new_option_names: list[str]
    ) -> bool:
        """
        Add new status options to a GitHub Project v2 Status field.

        Preserves all existing options and appends new ones.
        Returns True if successful.
        """
        # Build the complete options list (existing + new)
        # GitHub replaces ALL options with what you pass, so we must include existing ones
        all_options = []

        # First, include all existing options with their current properties
        for opt in existing_options:
            all_options.append({
                "name": opt["name"],
                "color": opt.get("color", "GRAY"),
                "description": opt.get("description", ""),
            })

        # Then add new options with default color
        for name in new_option_names:
            all_options.append({
                "name": name,
                "color": "GRAY",
                "description": "",
            })

        mutation = """
        mutation UpdateField($fieldId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
            updateProjectV2Field(input: {
                fieldId: $fieldId
                singleSelectOptions: $options
            }) {
                projectV2Field {
                    ... on ProjectV2SingleSelectField {
                        id
                        name
                        options {
                            id
                            name
                        }
                    }
                }
            }
        }
        """
        variables = {
            "fieldId": field_id,
            "options": all_options
        }

        result = self._graphql_request(mutation, variables)
        return result.get("updateProjectV2Field", {}).get("projectV2Field") is not None

    def add_issue_to_project(self, project_id: str, issue_node_id: str) -> Optional[str]:
        """
        Add an issue to a GitHub Project v2.

        Returns the project item ID if successful.
        """
        mutation = """
        mutation($projectId: ID!, $contentId: ID!) {
            addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
                item {
                    id
                }
            }
        }
        """
        variables = {"projectId": project_id, "contentId": issue_node_id}
        result = self._graphql_request(mutation, variables)

        item = result.get("addProjectV2ItemById", {}).get("item")
        return item.get("id") if item else None

    def set_project_item_status(
        self,
        project_id: str,
        item_id: str,
        status_field_id: str,
        status_option_id: str
    ) -> bool:
        """
        Set the status field value for a project item.

        Returns True if successful.
        """
        mutation = """
        mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
            updateProjectV2ItemFieldValue(input: {
                projectId: $projectId,
                itemId: $itemId,
                fieldId: $fieldId,
                value: {singleSelectOptionId: $optionId}
            }) {
                projectV2Item {
                    id
                }
            }
        }
        """
        variables = {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": status_field_id,
            "optionId": status_option_id
        }
        result = self._graphql_request(mutation, variables)

        return result.get("updateProjectV2ItemFieldValue", {}).get("projectV2Item") is not None

    def get_issue_node_id(self, repo_name: str, issue_number: int) -> Optional[str]:
        """Get the GraphQL node ID for an issue."""
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                issue(number: $number) {
                    id
                }
            }
        }
        """
        variables = {"owner": self.owner, "repo": repo_name, "number": issue_number}
        result = self._graphql_request(query, variables)

        issue = result.get("repository", {}).get("issue")
        return issue.get("id") if issue else None

    def get_owner_node_id(self) -> Optional[str]:
        """
        Get the GraphQL node ID for the owner (user or org).

        Required for creating new projects.
        """
        if self.is_org:
            result = self._make_request("GET", f"/orgs/{self.owner}")
        else:
            result = self._make_request("GET", f"/users/{self.owner}")

        if result:
            return result.get("node_id")
        return None

    def create_project_v2(self, title: str) -> Optional[dict]:
        """
        Create a new GitHub Project v2.

        Returns project info including id, number, and url.
        """
        owner_node_id = self.get_owner_node_id()
        if not owner_node_id:
            return None

        mutation = """
        mutation CreateProject($ownerId: ID!, $title: String!) {
            createProjectV2(input: {
                ownerId: $ownerId
                title: $title
            }) {
                projectV2 {
                    id
                    number
                    url
                    title
                }
            }
        }
        """
        variables = {
            "ownerId": owner_node_id,
            "title": title
        }

        result = self._graphql_request(mutation, variables)
        return result.get("createProjectV2", {}).get("projectV2")

    def link_repo_to_project(self, project_id: str, repo_name: str) -> bool:
        """
        Link a repository to a GitHub Project v2.

        This adds the repository to the project's linked repositories.
        """
        # First get the repo node ID
        query = """
        query($owner: String!, $repo: String!) {
            repository(owner: $owner, name: $repo) {
                id
            }
        }
        """
        variables = {"owner": self.owner, "repo": repo_name}
        result = self._graphql_request(query, variables)
        repo_id = result.get("repository", {}).get("id")

        if not repo_id:
            return False

        # Link the repo to the project
        mutation = """
        mutation LinkRepo($projectId: ID!, $repositoryId: ID!) {
            linkProjectV2ToRepository(input: {
                projectId: $projectId
                repositoryId: $repositoryId
            }) {
                repository {
                    id
                }
            }
        }
        """
        variables = {"projectId": project_id, "repositoryId": repo_id}
        result = self._graphql_request(mutation, variables)
        return result.get("linkProjectV2ToRepository", {}).get("repository") is not None

    def get_labels(self, repo_name: str) -> list[dict]:
        """Get all labels in a repository."""
        result = self._make_request(
            "GET",
            f"/repos/{self.owner}/{repo_name}/labels",
        )
        return result or []

    def create_label(self, repo_name: str, name: str, color: str) -> dict:
        """Create a label in a repository."""
        return self._make_request(
            "POST",
            f"/repos/{self.owner}/{repo_name}/labels",
            {"name": name, "color": color}
        )

    def create_issue(
        self,
        repo_name: str,
        title: str,
        body: str = "",
        milestone: Optional[int] = None,
        labels: Optional[list[str]] = None
    ) -> dict:
        """Create an issue in a repository."""
        cleaned_title = clean_title(title)

        data = {"title": cleaned_title, "body": body}

        if milestone:
            data["milestone"] = milestone
        if labels:
            data["labels"] = labels

        return self._make_request(
            "POST",
            f"/repos/{self.owner}/{repo_name}/issues",
            data
        )


# =============================================================================
# STREAMLIT UI
# =============================================================================

def init_session_state():
    """Initialize session state variables."""
    defaults = {
        "trello_connected": False,
        "boards": [],
        "selected_board": None,
        "board_data": None,
        "migration_complete": False,
        "migration_results": None,
        "input_mode": "api",  # "api" or "json"
        "json_loaded": False,
        # GitHub Project mapping
        "project_data": None,  # Project info from GitHub
        "status_field": None,  # Status field info (id, options)
        "status_mapping": {},  # Trello list ID -> mapping info
        "mapping_complete": False,
        "options_to_create": [],  # Status options that need to be created
        "project_mode": "existing",  # "existing" or "create_new"
        "new_project_name": "",  # Name for new project if creating
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def render_sidebar() -> dict:
    """Render the sidebar with credential inputs and input mode selection."""
    with st.sidebar:
        st.header("Data Source")

        # Input mode toggle
        input_mode = st.radio(
            "How would you like to import your Trello board?",
            options=["api", "json"],
            format_func=lambda x: "Connect via Trello API" if x == "api" else "Upload Board JSON",
            key="input_mode_selector",
            horizontal=False,
        )

        # Update session state
        if st.session_state.input_mode != input_mode:
            st.session_state.input_mode = input_mode
            # Reset board data when switching modes
            st.session_state.trello_connected = False
            st.session_state.boards = []
            st.session_state.board_data = None
            st.session_state.json_loaded = False

        st.divider()

        trello_api_key = ""
        trello_token = ""
        uploaded_file = None

        if input_mode == "api":
            # Trello API credentials
            st.subheader("Trello API")
            st.caption(
                "Get your API key and token from "
                "[Trello Developer Portal](https://trello.com/power-ups/admin)"
            )

            trello_api_key = st.text_input(
                "API Key",
                type="password",
                key="trello_api_key",
                help="Your Trello API key"
            )

            trello_token = st.text_input(
                "Token",
                type="password",
                key="trello_token",
                help="Your Trello API token"
            )

        else:
            # JSON upload mode
            st.subheader("Upload JSON")

            with st.expander("How to export from Trello", expanded=False):
                st.markdown("""
                1. Open your Trello board
                2. Click **Show Menu** (top right)
                3. Click **More**
                4. Click **Print and Export**
                5. Click **Export as JSON**
                6. Save the file and upload it below
                """)

            uploaded_file = st.file_uploader(
                "Board JSON file",
                type=["json"],
                key="trello_json_upload",
                help="Upload a Trello board export (.json)"
            )

            if uploaded_file:
                st.success(f"File loaded: {uploaded_file.name}")

        st.divider()

        # GitHub credentials (always shown)
        st.subheader("GitHub")

        # PAT scope warning
        with st.expander("Required token scopes", expanded=False):
            st.markdown("""
            Your GitHub token needs these scopes:
            - `repo` - Full control of repositories
            - `read:project` - Read project boards
            - `project` - Full control of projects

            [Generate token](https://github.com/settings/tokens/new?scopes=repo,read:project,project)
            """)

        github_token = st.text_input(
            "Personal Access Token",
            type="password",
            key="github_token",
            help="GitHub PAT with repo and project permissions"
        )

        # Account type toggle
        account_type = st.radio(
            "Account Type",
            options=["personal", "organization"],
            format_func=lambda x: "Personal Account" if x == "personal" else "Organization",
            key="github_account_type",
            horizontal=True,
        )

        is_org = account_type == "organization"

        github_owner = st.text_input(
            "Organization Name" if is_org else "Username",
            key="github_owner",
            help="GitHub organization name" if is_org else "Your GitHub username"
        )

        github_repo = st.text_input(
            "Repository Name",
            key="github_repo",
            help="Target repository name (will be created if it doesn't exist)"
        )

        # Project setup toggle
        st.write("**GitHub Project**")
        project_mode = st.radio(
            "Project Setup",
            options=["existing", "create_new"],
            format_func=lambda x: "Use existing project" if x == "existing" else "Create new project",
            key="project_mode",
            horizontal=True,
            label_visibility="collapsed"
        )

        github_project_number = ""
        github_project_name = ""

        if project_mode == "existing":
            github_project_number = st.text_input(
                "Project Number",
                key="github_project_number",
                help="The number from your project URL (e.g., 18 from /projects/18)"
            )

            # Guidance for existing project
            with st.expander("Need help finding your project?"):
                owner_placeholder = github_owner if github_owner else "YOUR_ORG"
                if is_org:
                    create_url = f"github.com/orgs/{owner_placeholder}/projects/new"
                    example_url = f"github.com/orgs/{owner_placeholder}/projects/18"
                else:
                    create_url = f"github.com/users/{owner_placeholder}/projects/new"
                    example_url = f"github.com/users/{owner_placeholder}/projects/18"

                st.markdown(f"""
**Don't have a project yet?** Create one at:
`{create_url}`

**To find your project number:**
Go to your GitHub Project board. The number is at the end of the URL:
`{example_url}` ← this is **18**

**Token permissions required:**
GitHub Settings → Developer Settings → Personal Access Tokens → Edit
Check: `read:project` and `project`
                """)
        else:
            github_project_name = st.text_input(
                "New Project Name",
                key="github_project_name",
                help="Name for the new GitHub Project to create"
            )
            st.info("A new GitHub Project will be created and linked to your repository during migration.")

        st.divider()

        # Validation status
        if project_mode == "existing":
            project_ready = bool(github_project_number)
        else:
            project_ready = bool(github_project_name)

        github_ready = all([github_token, github_owner, github_repo]) and project_ready

        if input_mode == "api":
            trello_ready = all([trello_api_key, trello_token])
        else:
            trello_ready = uploaded_file is not None

        if github_ready and trello_ready:
            st.success("Ready to proceed")
        else:
            missing = []
            if not trello_ready:
                missing.append("Trello data source")
            if not github_ready:
                missing.append("GitHub credentials")
            st.warning(f"Missing: {', '.join(missing)}")

        return {
            "input_mode": input_mode,
            "trello_api_key": trello_api_key,
            "trello_token": trello_token,
            "uploaded_file": uploaded_file,
            "github_token": github_token,
            "github_owner": github_owner,
            "github_repo": github_repo,
            "github_project_number": github_project_number,
            "github_project_name": github_project_name,
            "github_project_mode": project_mode,
            "github_is_org": is_org,
            "trello_ready": trello_ready,
            "github_ready": github_ready,
        }


def render_connect_step_api(trello_client: TrelloClient):
    """Render the Connect & Preview step for API mode."""
    st.header("Step 1: Connect & Preview")

    col1, col2 = st.columns([1, 3])

    with col1:
        if st.button("Connect to Trello", type="primary", use_container_width=True):
            with st.spinner("Fetching boards..."):
                try:
                    boards = trello_client.get_boards()
                    st.session_state.boards = boards
                    st.session_state.trello_connected = True
                    st.rerun()
                except requests.exceptions.HTTPError as e:
                    st.error(f"Failed to connect to Trello: {e}")
                except Exception as e:
                    st.error(f"Error: {e}")

    if st.session_state.trello_connected and st.session_state.boards:
        with col2:
            st.success(f"Connected! Found {len(st.session_state.boards)} boards")

        # Board selection
        board_options = {b["name"]: b["id"] for b in st.session_state.boards}
        selected_board_name = st.selectbox(
            "Select a board to migrate",
            options=list(board_options.keys()),
            key="board_selector"
        )

        if selected_board_name:
            selected_board_id = board_options[selected_board_name]

            if st.button("Load Board Data", use_container_width=False):
                with st.spinner("Fetching lists and cards..."):
                    try:
                        lists = trello_client.get_lists(selected_board_id)
                        cards = trello_client.get_cards(selected_board_id)
                        labels = trello_client.get_labels(selected_board_id)

                        # Organize cards by list
                        list_map = {lst["id"]: lst["name"] for lst in lists}

                        st.session_state.board_data = {
                            "board_id": selected_board_id,
                            "board_name": selected_board_name,
                            "lists": lists,
                            "cards": cards,
                            "labels": labels,
                            "list_map": list_map,
                        }
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error loading board: {e}")

        # Display board preview
        if st.session_state.board_data:
            render_board_preview(st.session_state.board_data)


def render_connect_step_json(uploaded_file):
    """Render the Connect & Preview step for JSON upload mode."""
    st.header("Step 1: Load & Preview")

    if not uploaded_file:
        st.info(
            "Upload a Trello board JSON export in the sidebar to preview your data."
        )

        with st.expander("How to export your Trello board as JSON", expanded=True):
            st.markdown("""
            ### Export Instructions

            1. Open your Trello board in a web browser
            2. Click the **Show Menu** button (top right corner)
            3. Click **More** to expand additional options
            4. Click **Print and Export**
            5. Click **Export as JSON**
            6. Save the downloaded `.json` file
            7. Upload it using the file uploader in the sidebar

            The JSON file contains all your lists, cards, labels, and descriptions.
            """)
        return

    # Parse the uploaded JSON
    col1, col2 = st.columns([1, 3])

    with col1:
        parse_button = st.button(
            "Parse JSON",
            type="primary",
            use_container_width=True,
            disabled=st.session_state.json_loaded
        )

    if parse_button or st.session_state.json_loaded:
        if not st.session_state.json_loaded:
            with st.spinner("Parsing board data..."):
                try:
                    json_content = uploaded_file.read()
                    json_data = json.loads(json_content)
                    board_data = parse_trello_json(json_data)
                    st.session_state.board_data = board_data
                    st.session_state.json_loaded = True
                    st.rerun()
                except json.JSONDecodeError as e:
                    st.error(f"Invalid JSON file: {e}")
                    return
                except Exception as e:
                    st.error(f"Error parsing board data: {e}")
                    return

        if st.session_state.board_data:
            with col2:
                st.success(
                    f"Loaded board: **{st.session_state.board_data['board_name']}**"
                )

            render_board_preview(st.session_state.board_data)

            # Option to reload
            if st.button("Clear and Upload Different File"):
                st.session_state.board_data = None
                st.session_state.json_loaded = False
                st.rerun()


def render_board_preview(board_data: dict):
    """Render the board preview with expandable lists."""
    st.subheader(f"Preview: {board_data['board_name']}")

    lists = board_data["lists"]
    cards = board_data["cards"]
    list_map = board_data["list_map"]

    # Group cards by list
    cards_by_list = {}
    for card in cards:
        list_id = card["idList"]
        if list_id not in cards_by_list:
            cards_by_list[list_id] = []
        cards_by_list[list_id].append(card)

    # Summary metrics
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Lists (→ Status)", len(lists))
    with col2:
        st.metric("Cards (→ Issues)", len(cards))
    with col3:
        unique_labels = set()
        for card in cards:
            for label in card.get("labels", []):
                if label.get("name"):
                    unique_labels.add(label["name"])
        st.metric("Labels", len(unique_labels))

    st.divider()

    # Expandable lists
    for lst in lists:
        list_cards = cards_by_list.get(lst["id"], [])
        with st.expander(f"**{lst['name']}** ({len(list_cards)} cards)", expanded=False):
            if list_cards:
                for card in list_cards:
                    st.markdown(f"**{card['name']}**")

                    # Show labels
                    labels = card.get("labels", [])
                    if labels:
                        label_names = [
                            f"`{l['name']}`"
                            for l in labels
                            if l.get("name")
                        ]
                        if label_names:
                            st.caption(f"Labels: {' '.join(label_names)}")

                    # Show description preview
                    desc = card.get("desc", "")
                    if desc:
                        preview = desc[:150] + "..." if len(desc) > 150 else desc
                        st.caption(preview)

                    st.markdown("---")
            else:
                st.caption("No cards in this list")


def render_status_mapping_step(
    github_client: GitHubClient,
    project_number: str,
    project_mode: str = "existing",
    project_name: str = ""
):
    """Render the status field mapping step."""
    st.header("Step 2: Map Lists to Project Status")

    if not st.session_state.board_data:
        st.info("Please load a Trello board first.")
        return False

    board_data = st.session_state.board_data
    lists = board_data["lists"]

    # Handle "create new project" mode
    if project_mode == "create_new":
        if not project_name:
            st.warning("Please enter a name for the new GitHub Project in the sidebar.")
            return False

        st.info(f"A new project **\"{project_name}\"** will be created during migration.")
        st.write("All Trello lists will become Status options in the new project.")

        # Build automatic mapping (all lists become new status options)
        mapping = {}
        options_to_create = []

        st.divider()
        st.write("**Status Mapping Preview:**")

        for lst in lists:
            list_name = lst["name"]
            cleaned_name = clean_title(list_name)

            col1, col2, col3 = st.columns([2, 2, 1])

            with col1:
                st.write(f"**{list_name}**")
            with col2:
                st.write(f"→ {cleaned_name}")
            with col3:
                st.warning("Will create", icon="➕")

            mapping[lst["id"]] = {
                "status_name": cleaned_name,
                "status_option_id": None,
                "needs_creation": True
            }
            if cleaned_name not in options_to_create:
                options_to_create.append(cleaned_name)

        st.session_state.options_to_create = options_to_create
        st.session_state.project_mode = "create_new"
        st.session_state.new_project_name = project_name

        st.divider()

        if st.button("Confirm Mapping", type="primary"):
            st.session_state.status_mapping = mapping
            st.session_state.mapping_complete = True
            st.rerun()

        if st.session_state.mapping_complete:
            st.success("Mapping confirmed. Ready to migrate.")

        return st.session_state.mapping_complete

    # Handle "existing project" mode
    if not project_number:
        st.warning("Please enter a GitHub Project Number in the sidebar.")
        return False

    # Fetch project data if not already loaded
    if not st.session_state.project_data:
        with st.spinner("Fetching GitHub Project..."):
            try:
                project_num = int(project_number)
                project = github_client.get_project_v2(project_num)

                if not project:
                    st.error(
                        f"Project #{project_number} not found. "
                        "Make sure the project exists and your token has project access."
                    )
                    return False

                st.session_state.project_data = project

                # Get status field
                status_field = github_client.get_project_status_field(project["id"])
                if not status_field:
                    st.error(
                        "No Status field found in the project. "
                        "Please add a Status field to your GitHub Project."
                    )
                    return False

                st.session_state.status_field = status_field

            except ValueError:
                st.error("Project number must be a number.")
                return False
            except Exception as e:
                st.error(f"Error fetching project: {e}")
                return False

    project = st.session_state.project_data
    status_field = st.session_state.status_field

    if not project or not status_field:
        return False

    st.session_state.project_mode = "existing"
    st.success(f"Connected to project: **{project['title']}**")

    # Get available status options
    status_options = status_field.get("options", [])
    option_map = {opt["name"]: opt["id"] for opt in status_options}

    # Identify which lists have matches and which need to be created
    lists_to_create = []
    lists_matched = []

    for lst in lists:
        cleaned_name = clean_title(lst["name"])
        if cleaned_name in option_map:
            lists_matched.append(cleaned_name)
        else:
            lists_to_create.append(cleaned_name)

    # Show summary
    if lists_to_create:
        st.info(
            f"**{len(lists_to_create)} new Status options will be created:** "
            f"{', '.join(lists_to_create)}"
        )

    if lists_matched:
        st.write(f"**{len(lists_matched)} lists match existing Status options**")

    st.divider()
    st.write("**Status Mapping Preview:**")

    # Build mapping automatically
    mapping = {}
    options_to_create = []

    for lst in lists:
        list_name = lst["name"]
        cleaned_name = clean_title(list_name)

        col1, col2, col3 = st.columns([2, 2, 1])

        with col1:
            st.write(f"**{list_name}**")

        with col2:
            if cleaned_name in option_map:
                st.write(f"→ {cleaned_name}")
                mapping[lst["id"]] = {
                    "status_name": cleaned_name,
                    "status_option_id": option_map[cleaned_name],
                    "needs_creation": False
                }
            else:
                st.write(f"→ {cleaned_name}")
                mapping[lst["id"]] = {
                    "status_name": cleaned_name,
                    "status_option_id": None,  # Will be set after creation
                    "needs_creation": True
                }
                if cleaned_name not in options_to_create:
                    options_to_create.append(cleaned_name)

        with col3:
            if cleaned_name in option_map:
                st.success("Matched", icon="✅")
            else:
                st.warning("Will create", icon="➕")

    # Store options that need to be created
    st.session_state.options_to_create = options_to_create

    st.divider()

    # Save mapping button
    if st.button("Confirm Mapping", type="primary"):
        st.session_state.status_mapping = mapping
        st.session_state.mapping_complete = True
        st.rerun()

    return st.session_state.mapping_complete


def render_migrate_step(
    github_client: GitHubClient,
    repo_name: str,
    project_number: str
):
    """Render the migration step."""
    st.header("Step 3: Migrate to GitHub")

    if not st.session_state.board_data:
        if st.session_state.input_mode == "api":
            st.info("Please connect to Trello and load a board first.")
        else:
            st.info("Please upload and parse a Trello board JSON file first.")
        return

    if not st.session_state.mapping_complete:
        st.info("Please complete the status mapping in Step 2 first.")
        return

    board_data = st.session_state.board_data
    project = st.session_state.project_data
    project_mode = st.session_state.get("project_mode", "existing")
    new_project_name = st.session_state.get("new_project_name", "")

    st.write(
        f"Ready to migrate **{board_data['board_name']}** to "
        f"**{github_client.owner}/{repo_name}**"
    )

    # Migration summary
    mapped_count = len(st.session_state.status_mapping)

    with st.container(border=True):
        st.write("**Migration plan:**")
        st.write(f"- Create repository `{repo_name}` (if it doesn't exist)")
        if project_mode == "create_new":
            st.write(f"- Create new GitHub Project: **{new_project_name}**")
            st.write(f"- Link repository to project")
        else:
            st.write(f"- Add issues to project: **{project['title']}**")
        st.write(f"- Create {len(board_data['cards'])} issues")
        st.write(f"- Set status for {mapped_count} list mappings")

    col1, col2 = st.columns([1, 3])

    with col1:
        migrate_button = st.button(
            "Start Migration",
            type="primary",
            use_container_width=True
        )

    if migrate_button:
        run_migration(github_client, repo_name, board_data)


def run_migration(
    github_client: GitHubClient,
    repo_name: str,
    board_data: dict
):
    """Execute the migration process using GitHub Projects v2."""
    project = st.session_state.project_data
    status_field = st.session_state.status_field
    status_mapping = st.session_state.status_mapping.copy()  # Make a copy to modify
    options_to_create = st.session_state.get("options_to_create", [])
    project_mode = st.session_state.get("project_mode", "existing")
    new_project_name = st.session_state.get("new_project_name", "")

    results = {
        "repo_created": False,
        "project_created": False,
        "project_url": None,
        "status_options_created": 0,
        "labels_created": 0,
        "issues_created": 0,
        "issues_added_to_project": 0,
        "statuses_set": 0,
        "errors": [],
        "repo_url": f"https://github.com/{github_client.owner}/{repo_name}",
    }

    cards = board_data["cards"]

    # Total steps: repo + project (if creating) + status options + labels + (issue creation + add to project + set status) per card
    project_steps = 1 if project_mode == "create_new" else 0
    total_steps = 1 + project_steps + 1 + 1 + (len(cards) * 3)
    current_step = 0

    progress_bar = st.progress(0)
    status_text = st.empty()
    error_container = st.container()

    try:
        # Step 1: Create or verify repository
        status_text.write("Checking repository...")

        if not github_client.repo_exists(repo_name):
            status_text.write(f"Creating repository: {repo_name}")
            github_client.create_repo(
                repo_name,
                f"Migrated from Trello board: {board_data['board_name']}"
            )
            results["repo_created"] = True
            time.sleep(2)  # Brief pause for repo creation to propagate

        current_step += 1
        progress_bar.progress(current_step / total_steps)

        # Step 1.5: Create new project if needed
        if project_mode == "create_new":
            status_text.write(f"Creating GitHub Project: {new_project_name}...")

            try:
                project = github_client.create_project_v2(new_project_name)

                if project:
                    results["project_created"] = True
                    results["project_url"] = project.get("url")
                    st.session_state.project_data = project

                    status_text.write(f"Created project: {project['title']}")

                    # Link repo to project
                    status_text.write("Linking repository to project...")
                    github_client.link_repo_to_project(project["id"], repo_name)

                    # Get the status field for the new project
                    time.sleep(1)  # Brief pause for propagation
                    status_field = github_client.get_project_status_field(project["id"])

                    if status_field:
                        st.session_state.status_field = status_field
                    else:
                        # Create a default status field response for new projects
                        # New projects have a Status field by default
                        error_msg = "Could not find Status field in new project"
                        results["errors"].append(error_msg)
                        with error_container:
                            st.warning(error_msg)
                else:
                    error_msg = "Failed to create GitHub Project"
                    results["errors"].append(error_msg)
                    with error_container:
                        st.error(error_msg)
                    return

            except Exception as e:
                error_msg = f"Error creating project: {e}"
                results["errors"].append(error_msg)
                with error_container:
                    st.error(error_msg)
                return

            current_step += 1
            progress_bar.progress(current_step / total_steps)

        # Step 2: Create new Status options if needed
        if options_to_create:
            status_text.write(f"Creating {len(options_to_create)} new Status options...")

            try:
                existing_options = status_field.get("options", [])
                success = github_client.create_status_options(
                    status_field["id"],
                    existing_options,
                    options_to_create
                )

                if success:
                    results["status_options_created"] = len(options_to_create)
                    status_text.write(f"Created Status options: {', '.join(options_to_create)}")

                    # Re-query the status field to get the new option IDs
                    time.sleep(1)  # Brief pause for propagation
                    updated_status_field = github_client.get_project_status_field(project["id"])

                    if updated_status_field:
                        st.session_state.status_field = updated_status_field
                        status_field = updated_status_field

                        # Build new option map with fresh IDs
                        new_option_map = {
                            opt["name"]: opt["id"]
                            for opt in updated_status_field.get("options", [])
                        }

                        # Update mapping with new option IDs
                        for list_id, mapping_info in status_mapping.items():
                            if mapping_info.get("needs_creation"):
                                status_name = mapping_info["status_name"]
                                if status_name in new_option_map:
                                    mapping_info["status_option_id"] = new_option_map[status_name]
                                    mapping_info["needs_creation"] = False
                else:
                    error_msg = "Failed to create Status options"
                    results["errors"].append(error_msg)
                    with error_container:
                        st.error(error_msg)

            except Exception as e:
                error_msg = f"Error creating Status options: {e}"
                results["errors"].append(error_msg)
                with error_container:
                    st.error(error_msg)

        current_step += 1
        progress_bar.progress(current_step / total_steps)

        # Step 3: Create labels
        status_text.write("Creating labels...")

        existing_labels = github_client.get_labels(repo_name)
        existing_label_names = {l["name"].lower() for l in existing_labels} if existing_labels else set()

        unique_labels = {}
        for card in cards:
            for label in card.get("labels", []):
                if label.get("name"):
                    unique_labels[label["name"]] = label.get("color")

        for label_name, label_color in unique_labels.items():
            if label_name.lower() not in existing_label_names:
                try:
                    color = github_client.COLOR_MAP.get(label_color, "ededed")
                    github_client.create_label(repo_name, label_name, color)
                    results["labels_created"] += 1
                except Exception as e:
                    error_msg = f"Failed to create label '{label_name}': {e}"
                    results["errors"].append(error_msg)
                    with error_container:
                        st.error(error_msg)

        current_step += 1
        progress_bar.progress(current_step / total_steps)

        # Step 3: Create issues and add to project
        status_text.write("Creating issues and adding to project...")

        for card in cards:
            card_name = card["name"]
            cleaned_card_name = clean_title(card_name)
            card_desc = card.get("desc", "")
            list_id = card["idList"]
            card_labels = [l["name"] for l in card.get("labels", []) if l.get("name")]

            # Build issue body with Trello reference
            body = card_desc
            if card.get("url"):
                body += f"\n\n---\n*Migrated from Trello: {card['url']}*"

            issue_number = None
            issue_node_id = None

            # 3a: Create the issue
            try:
                issue = github_client.create_issue(
                    repo_name,
                    card_name,
                    body,
                    labels=card_labels if card_labels else None
                )
                if issue and "number" in issue:
                    issue_number = issue["number"]
                    results["issues_created"] += 1
                    status_text.write(f"Created issue #{issue_number}: {cleaned_card_name}")
            except Exception as e:
                error_msg = f"Failed to create issue '{cleaned_card_name}': {e}"
                results["errors"].append(error_msg)
                with error_container:
                    st.error(error_msg)

            current_step += 1
            progress_bar.progress(current_step / total_steps)

            # 3b: Add issue to project
            if issue_number:
                try:
                    # Get the issue's node ID for GraphQL
                    issue_node_id = github_client.get_issue_node_id(repo_name, issue_number)

                    if issue_node_id:
                        project_item_id = github_client.add_issue_to_project(
                            project["id"],
                            issue_node_id
                        )
                        if project_item_id:
                            results["issues_added_to_project"] += 1
                            status_text.write(f"Added issue #{issue_number} to project")

                            # 3c: Set status field
                            if list_id in status_mapping:
                                mapping = status_mapping[list_id]
                                try:
                                    success = github_client.set_project_item_status(
                                        project["id"],
                                        project_item_id,
                                        status_field["id"],
                                        mapping["status_option_id"]
                                    )
                                    if success:
                                        results["statuses_set"] += 1
                                        status_text.write(
                                            f"Set status to '{mapping['status_name']}' for issue #{issue_number}"
                                        )
                                except Exception as e:
                                    error_msg = f"Failed to set status for issue #{issue_number}: {e}"
                                    results["errors"].append(error_msg)
                                    with error_container:
                                        st.warning(error_msg)

                except Exception as e:
                    error_msg = f"Failed to add issue #{issue_number} to project: {e}"
                    results["errors"].append(error_msg)
                    with error_container:
                        st.warning(error_msg)

            current_step += 2  # Count both project add and status set steps
            progress_bar.progress(min(current_step / total_steps, 1.0))

            # Small delay to avoid rate limiting
            time.sleep(0.5)

        progress_bar.progress(1.0)
        status_text.write("Migration complete!")

    except Exception as e:
        results["errors"].append(f"Migration failed: {e}")
        status_text.error(f"Migration error: {e}")

    st.session_state.migration_complete = True
    st.session_state.migration_results = results
    st.rerun()


def render_results():
    """Render the migration results."""
    st.header("Migration Results")

    results = st.session_state.migration_results

    if not results:
        return

    # Success metrics
    col1, col2, col3, col4, col5, col6 = st.columns(6)

    with col1:
        st.metric("Repository", "Created" if results["repo_created"] else "Existed")
    with col2:
        if results.get("project_created"):
            st.metric("Project", "Created")
        else:
            st.metric("Status Options", results.get("status_options_created", 0))
    with col3:
        st.metric("Issues", results["issues_created"])
    with col4:
        st.metric("Labels", results["labels_created"])
    with col5:
        st.metric("In Project", results.get("issues_added_to_project", 0))
    with col6:
        st.metric("Status Set", results.get("statuses_set", 0))

    # Errors
    if results["errors"]:
        st.subheader("Errors")
        for error in results["errors"]:
            st.error(error)
    else:
        st.success("Migration completed successfully!")

    # Links
    st.divider()
    st.subheader("Your Resources")
    st.markdown(f"**Repository:** [{results['repo_url']}]({results['repo_url']})")

    if results.get("project_url"):
        st.markdown(f"**Project:** [{results['project_url']}]({results['project_url']})")

    # Reset button
    if st.button("Start New Migration"):
        st.session_state.trello_connected = False
        st.session_state.boards = []
        st.session_state.selected_board = None
        st.session_state.board_data = None
        st.session_state.migration_complete = False
        st.session_state.migration_results = None
        st.session_state.json_loaded = False
        st.session_state.project_data = None
        st.session_state.status_field = None
        st.session_state.status_mapping = {}
        st.session_state.mapping_complete = False
        st.session_state.options_to_create = []
        st.session_state.project_mode = "existing"
        st.session_state.new_project_name = ""
        st.rerun()


def main():
    """Main application entry point."""
    st.set_page_config(
        page_title="Trello → GitHub Migrator",
        page_icon="🔄",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()

    # Header
    st.title("Trello → GitHub Migrator")
    st.caption(
        "Migrate your Trello boards to GitHub Issues and Projects with ease."
    )

    st.divider()

    # Sidebar - returns dict with all settings
    sidebar_config = render_sidebar()

    input_mode = sidebar_config["input_mode"]
    trello_ready = sidebar_config["trello_ready"]
    github_ready = sidebar_config["github_ready"]

    # Check if ready to proceed
    if not github_ready:
        st.info(
            "Please fill in your GitHub credentials in the sidebar to get started."
        )

        with st.expander("How to get GitHub credentials", expanded=True):
            st.markdown("""
            ### GitHub Personal Access Token

            1. Go to [GitHub Settings → Developer Settings → Personal Access Tokens](https://github.com/settings/tokens)
            2. Click "Generate new token (classic)"
            3. Give it a descriptive name
            4. Select these scopes:
               - `repo` - Full control of repositories
               - `read:project` - Read project boards
               - `project` - Full control of projects
            5. Click "Generate token" and copy it immediately

            ### GitHub Username, Repository & Project

            - **Username**: Your GitHub username (e.g., `octocat`)
            - **Repository Name**: The name for the new repo (will be created if it doesn't exist)
            - **Project Number**: The number from your project URL (e.g., `18` from `/projects/18`)
            """)
        return

    if not trello_ready and input_mode == "api":
        st.info(
            "Please fill in your Trello API credentials in the sidebar."
        )

        with st.expander("How to get Trello API credentials", expanded=True):
            st.markdown("""
            ### Trello API Credentials

            1. Go to [Trello Power-Ups Admin](https://trello.com/power-ups/admin)
            2. Click "New" to create a new Power-Up (or use an existing one)
            3. Copy your **API Key** from the API Key section
            4. Click "Generate a Token" and authorize the app
            5. Copy the **Token** that appears
            """)
        return

    # Initialize GitHub client
    github_client = GitHubClient(
        sidebar_config["github_token"],
        sidebar_config["github_owner"],
        sidebar_config["github_is_org"]
    )
    github_repo = sidebar_config["github_repo"]
    github_project_number = sidebar_config["github_project_number"]
    github_project_name = sidebar_config["github_project_name"]
    github_project_mode = sidebar_config["github_project_mode"]

    # Show results if migration is complete
    if st.session_state.migration_complete:
        render_results()
        return

    # Main workflow based on input mode
    # Step 1: Connect to Trello / Load JSON
    if input_mode == "api":
        trello_client = TrelloClient(
            sidebar_config["trello_api_key"],
            sidebar_config["trello_token"]
        )
        render_connect_step_api(trello_client)
    else:
        render_connect_step_json(sidebar_config["uploaded_file"])

    st.divider()

    # Step 2: Map Trello lists to GitHub Project Status
    if st.session_state.board_data:
        render_status_mapping_step(
            github_client,
            github_project_number,
            github_project_mode,
            github_project_name
        )

        st.divider()

    # Step 3: Migrate
    render_migrate_step(github_client, github_repo, github_project_number)


if __name__ == "__main__":
    main()
