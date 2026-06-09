#!/bin/bash
echo "Starting Decoupled Frontend Agents..."
podman compose -f docker-compose.frontend-agents.yml up -d --build
echo "Agents are running. Monitoring queue:frontend..."
