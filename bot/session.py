"""User session and project management."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_projects() -> dict:
    """Load projects from PROJECTS_JSON env var or use defaults.

    Set PROJECTS_JSON to a JSON object mapping project keys to
    {"name": "Display Name", "path": "/absolute/path/to/project"}.
    """
    projects_json = os.environ.get("PROJECTS_JSON")
    if projects_json:
        return json.loads(projects_json)
    return {
        "example": {
            "name": "📁 Example Project",
            "path": os.path.expanduser("~/projects/example"),
        },
    }


PROJECTS = _load_projects()

STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"


@dataclass
class ProjectSession:
    session_id: str
    mode: str = "discuss"  # "discuss" or "work"


@dataclass
class UserState:
    current_project: str | None = None
    sessions: dict[str, ProjectSession] = field(default_factory=dict)

    def get_session(self) -> ProjectSession | None:
        if not self.current_project:
            return None
        return self.sessions.get(self.current_project)

    def ensure_session(self) -> ProjectSession:
        if not self.current_project:
            raise ValueError("No project selected")
        if self.current_project not in self.sessions:
            self.sessions[self.current_project] = ProjectSession(
                session_id=""  # Will be set from Claude's response
            )
        return self.sessions[self.current_project]

    def new_session(self) -> ProjectSession:
        if not self.current_project:
            raise ValueError("No project selected")
        self.sessions[self.current_project] = ProjectSession(
            session_id=""  # Will be set from Claude's response
        )
        return self.sessions[self.current_project]

    def set_project(self, project_key: str) -> None:
        if project_key not in PROJECTS:
            raise ValueError(f"Unknown project: {project_key}")
        self.current_project = project_key

    def set_work_mode(self) -> None:
        session = self.ensure_session()
        session.mode = "work"

    def set_discuss_mode(self) -> None:
        session = self.get_session()
        if session:
            session.mode = "discuss"

    @property
    def project_path(self) -> str | None:
        if not self.current_project:
            return None
        return PROJECTS[self.current_project]["path"]

    @property
    def project_name(self) -> str | None:
        if not self.current_project:
            return None
        return PROJECTS[self.current_project]["name"]

    @property
    def is_work_mode(self) -> bool:
        session = self.get_session()
        return session is not None and session.mode == "work"


def save_state(state: UserState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "current_project": state.current_project,
        "sessions": {
            k: {"session_id": v.session_id, "mode": v.mode}
            for k, v in state.sessions.items()
        },
    }
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_state() -> UserState:
    if not STATE_FILE.exists():
        return UserState()
    try:
        data = json.loads(STATE_FILE.read_text())
        state = UserState(
            current_project=data.get("current_project"),
        )
        for k, v in data.get("sessions", {}).items():
            state.sessions[k] = ProjectSession(
                session_id=v["session_id"],
                mode=v.get("mode", "discuss"),
            )
        return state
    except Exception:
        logger.exception("Failed to load state")
        return UserState()
