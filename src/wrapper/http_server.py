import json
import subprocess
import asyncio
from fastapi import FastAPI, HTTPException

app = FastAPI()

@app.post("/mcp")
async def mcp_endpoint(payload: dict):
    try:
        # Lanzar tu MCP server como proceso hijo
        proc = await asyncio.create_subprocess_exec(
            "python", "-m", "mcp_yfinance",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Enviar JSON-RPC al MCP server
        input_data = json.dumps(payload).encode()
        stdout, stderr = await proc.communicate(input_data)

        if stderr:
            return {"error": stderr.decode()}

        # Devolver respuesta JSON-RPC
        return json.loads(stdout.decode())

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
