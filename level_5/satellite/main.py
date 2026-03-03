import asyncio
import json
import random
import logging
import ssl
import os
from dotenv import load_dotenv

# Load env from project root
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel

# A2A Imports
from a2a.client.transports.kafka import KafkaClientTransport
from a2a.client.middleware import ClientCallContext
from a2a.types import (
    AgentCard,
    AgentCapabilities,
    MessageSendParams,
    Message,
    Task,
)


# Configure Logging
logging.basicConfig(level=logging.INFO)
# logging.getLogger("aiokafka").setLevel(logging.DEBUG)
logger = logging.getLogger("satellite_dashboard")
logger.setLevel(logging.INFO)

from contextlib import asynccontextmanager

#REPLACE-CONNECT-TO-KAFKA-CLUSTER
@asynccontextmanager
async def lifespan(app: FastAPI):
    global kafka_transport
    logger.info("Initializing Kafka Client Transport...")
    
    bootstrap_server = os.getenv("KAFKA_BOOTSTRAP_SERVERS")
    request_topic = "a2a-formation-request"
    reply_topic = "a2a-reply-satellite-dashboard"
    
    # Create AgentCard for the Client
    client_card = AgentCard(
        name="SatelliteDashboard",
        description="Satellite Dashboard Client",
        version="1.0.0",
        url="https://example.com/satellite-dashboard",
        capabilities=AgentCapabilities(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[]
    )
    
    kafka_transport = KafkaClientTransport(
            agent_card=client_card,
            bootstrap_servers=bootstrap_server,
            request_topic=request_topic,
            reply_topic=reply_topic,
    )
    
    try:
        await kafka_transport.start()
        logger.info("Kafka Client Transport Started Successfully.")
    except Exception as e:
        logger.error(f"Failed to start Kafka Client: {e}")
        
    yield
    
    if kafka_transport:
        logger.info("Stopping Kafka Client Transport...")
        await kafka_transport.stop()
        logger.info("Kafka Client Transport Stopped.")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.cloudshell\.dev|http://localhost.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# State
# Pods: 15 items. Default freeform random.
PODS = []
TARGET_PODS = []
FORMATION = "FREEFORM"

# Global Transport
kafka_transport = None

class FormationRequest(BaseModel):
    formation: str

def init_pods():
    global PODS, TARGET_PODS
    PODS = [{"id": i, "x": random.randint(50, 850), "y": random.randint(100, 600)} for i in range(15)]
    TARGET_PODS = [p.copy() for p in PODS]

init_pods()

#REPLACE-SSE-STREAM
@app.get("/stream")
async def message_stream(request: Request):
    async def event_generator():
        logger.info("New SSE stream connected")
        try:
            while True:
                current_pods = list(PODS) 
                
                # Send updates one by one to simulate low-bandwidth scanning
                for pod in current_pods:
                     payload = {"pod": pod}
                     yield {
                         "event": "pod_update",
                         "data": json.dumps(payload)
                     }
                     await asyncio.sleep(0.02)
                
                # Send formation info occasionally
                yield {
                    "event": "formation_update",
                    "data": json.dumps({"formation": FORMATION})
                }
                
                # Main loop delay
                await asyncio.sleep(0.5)
                
        except asyncio.CancelledError:
             logger.info("SSE stream disconnected (cancelled)")
        except Exception as e:
             logger.error(f"SSE stream error: {e}")
             
    return EventSourceResponse(event_generator())

#REPLACE-FORMATION-REQUEST
@app.post("/formation")
async def set_formation(req: FormationRequest):
    global FORMATION, PODS
    FORMATION = req.formation
    logger.info(f"Received formation request: {FORMATION}")
    
    if not kafka_transport:
        logger.error("Kafka Transport is not initialized!")
        return {"status": "error", "message": "Backend Not Connected"}
    
    try:
        # Construct A2A Message
        prompt = f"Create a {FORMATION} formation"
        logger.info(f"Sending A2A Message: '{prompt}'")
        
        from a2a.types import TextPart, Part, Role
        import uuid
        
        msg_id = str(uuid.uuid4())
        message_parts = [Part(TextPart(text=prompt))]
        
        msg_obj = Message(
            message_id=msg_id,
            role=Role.user,
            parts=message_parts
        )
        
        message_params = MessageSendParams(
            message=msg_obj
        )
        
        # Send and Wait for Response
        ctx = ClientCallContext()
        ctx.state["kafka_timeout"] = 120.0 # Timeout for GenAI latency
        response = await kafka_transport.send_message(message_params, context=ctx)
        
        logger.info("Received A2A Response.")
        
        content = None
        if isinstance(response, Message):
            content = response.parts[0].root.text if response.parts else None
        elif isinstance(response, Task):
            if response.artifacts and response.artifacts[0].parts:
                content = response.artifacts[0].parts[0].root.text

        if content:
            logger.info(f"Response Content: {content[:100]}...")
            try:
                clean_content = content.replace("```json", "").replace("```", "").strip()
                coords = json.loads(clean_content)
                
                if isinstance(coords, list):
                    logger.info(f"Parsed {len(coords)} coordinates.")
                    for i, pod_target in enumerate(coords):
                        if i < len(PODS):
                            PODS[i]["x"] = pod_target["x"]
                            PODS[i]["y"] = pod_target["y"]
                    return {"status": "success", "formation": FORMATION}
                else:
                    logger.error("Response JSON is not a list.")
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Agent JSON response: {e}")
        else:
            logger.error(f"Could not extract content from response type {type(response)}")

    except Exception as e:
        logger.error(f"Error calling agent via Kafka: {e}")
        return {"status": "error", "message": str(e)}

class PodUpdate(BaseModel):
    id: int
    x: int
    y: int

@app.post("/update_pod")
async def update_pod_manual(update: PodUpdate):
    """Manual override for drag-and-drop."""
    global FORMATION
    FORMATION = "RANDOM"
    
    # Find the pod and update both current and target to stop it from drifting back
    # effectively "teleporting" it or re-anchoring it.
    found = False
    for p in PODS:
        if p["id"] == update.id:
            p["x"] = update.x
            p["y"] = update.y
            found = True
            break
            
    for t in TARGET_PODS:
        if t["id"] == update.id:
            t["x"] = update.x
            t["y"] = update.y
            break
            
    if found:
        # Force immediate update push?
        # The stream loop will pick it up, but we could trigger it.
        pass
        
    return {"status": "updated", "id": update.id}

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Ensure API routes are above this!

# Serve Static Assets (JS/CSS)
# We assume the user has run 'npm run build' in ../frontend
# resulting in ../frontend/dist
dist_dir = os.path.join(os.path.dirname(__file__), "../frontend/dist")

if os.path.exists(dist_dir):
    app.mount("/assets", StaticFiles(directory=os.path.join(dist_dir, "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_react_app(full_path: str):
        # 1. If it matches an underlying file (like favicon.svg), serve it
        possible_file = os.path.join(dist_dir, full_path)
        if os.path.isfile(possible_file):
            return FileResponse(possible_file)
        
        # 2. Otherwise return index.html for SPA routing
        return FileResponse(os.path.join(dist_dir, "index.html"))
else:
    logger.warning("Frontend build not found. Please run 'npm run build' in frontend/.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, proxy_headers=True, forwarded_allow_ips="*")
