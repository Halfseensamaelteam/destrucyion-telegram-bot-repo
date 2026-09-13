#!/bin/bash
# Helper script to export uv dependencies to requirements.txt for Vercel deployment.

echo "Exporting dependencies to requirements.txt..."
uv export --format requirements-txt > requirements.txt
echo "Done! requirements.txt has been updated."
