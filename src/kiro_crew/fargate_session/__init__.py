"""Fargate session environments: a session's workspace in an ECS task.

The engine stays on the gateway, as for Coder; the task holds the workspace.
:mod:`.bootstrap` speaks to the guest bootstrap, :mod:`.tasks` runs and stops
ECS tasks, :mod:`.forward` keeps the SSM port forward, and :mod:`.launch` ties
them into the same ``prepare`` seam Coder's provider offers.
"""
