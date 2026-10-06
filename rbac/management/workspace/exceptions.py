"""Workspace domain exceptions."""

WORKSPACE_NOT_EMPTY_CODE = "insights-rbac.workspace.not-empty"
WORKSPACE_NOT_EMPTY_TITLE = "Unable to delete due to workspace dependencies"
WORKSPACE_NOT_EMPTY_PROBLEM_TYPE = "http://project-kessel.org/problems/workspace-not-empty"


class WorkspaceNotEmptyError(Exception):
    """Raised when a workspace has dependent workspaces and cannot be deleted."""
