# Node 22 LTS: Node 20 left maintenance in April 2026, and vite 7 requires
# ^20.19.0 || >=22.12.0, so 20.x only barely qualified. Keep this in sync with
# the "engines" field in frontend/package.json.
FROM node:22-slim AS builder

# Set the working directory for our build process
WORKDIR /app

# Copy the frontend's package files first to leverage Docker's layer caching.
# The glob picks up package-lock.json too, which `npm ci` requires.
COPY frontend/package*.json ./frontend/
# `npm ci` installs exactly the locked tree. `npm install` re-resolves every
# caret range, so a new minor of vite/react/tailwind would land in the image
# with no repo change and the lockfile we tested against would be ignored.
RUN npm --prefix frontend ci

# Copy the rest of the frontend source code
COPY frontend/ ./frontend/
# Run the build script, which will create the 'frontend/dist' directory
RUN npm --prefix frontend run build


# STAGE 2: Build the Python Production Image
# This stage creates the final, lean container with our Python app and the built frontend.
FROM python:3.13-slim

# Set the final working directory
WORKDIR /app

# Install uv, our fast package manager
RUN pip install uv

# Copy the requirements.txt from the backend directory
COPY requirements.txt overrides.txt ./
# Install the Python dependencies.
#
# --override is required, not optional: requirements.txt pins websockets above
# the caps google-adk (<16) and google-genai (<17) declare, so a plain
# `uv pip install -r requirements.txt` fails with "No solution found ... your
# requirements are unsatisfiable" and the image never builds. See overrides.txt.
RUN uv pip install --no-cache-dir --system --override overrides.txt -r requirements.txt

# Copy the contents of your backend application directory directly into the working directory.
COPY backend/app/ .

# CRITICAL STEP: Copy the built frontend assets from the 'builder' stage.
# We copy to /frontend/dist because main.py looks for "../../frontend/dist"
# When main.py is in /app, "../../" resolves to "/", so it looks for /frontend/dist
COPY --from=builder /app/frontend/dist /frontend/dist

# Cloud Run injects PORT; main.py now reads it (defaulting to 8080) rather than
# hardcoding the value and happening to agree.
EXPOSE 8080

# Set the command to run the application.
CMD ["python", "main.py"]
