import os, sys
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
import uvicorn

port = int(os.environ.get("PORT", 7860))
uvicorn.run("main:app", host="0.0.0.0", port=port, loop="asyncio")
