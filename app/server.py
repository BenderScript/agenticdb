import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict

import openai
import yaml
from dotenv import load_dotenv, find_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langserve import add_routes


def load_env_file():
    # Try to find and load the default .env file first
    env_path = find_dotenv()
    if env_path != "":
        load_dotenv(dotenv_path=env_path, override=True)
        logging.info(f"Loaded environment variables from {env_path}")
    else:
        # If the default .env file is not found, try to find and load .env.azure
        env_azure_path = find_dotenv(".env.azure")
        if env_azure_path:
            load_dotenv(dotenv_path=env_azure_path, override=True)
            logging.info(f"Loaded environment variables from {env_azure_path}")
        else:
            logging.error("Neither .env nor .env.azure files were found")
            raise FileNotFoundError("Neither .env nor .env.azure files were found")


def get_text_splitter():
    return RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=0,
        length_function=len,
        is_separator_regex=False,
        separators=[
            "\n\n",
            "\n",
        ],
    )


def get_embedding_function():
    return OpenAIEmbeddings(model="text-embedding-3-large")


class AppState:
    def __init__(self):
        self.text_splitter = None
        self.embedding_function = None
        self.agents_db = None
        self.ratings_db = None


class YAMLContent(BaseModel):
    original_content: str
    parsed_content: Dict[str, Any]

    @field_validator('parsed_content', mode='before')
    def parse_yaml(cls, v, values):
        try:
            return yaml.safe_load(values['original_content'])
        except yaml.YAMLError as e:
            logging.error(f"Failed to parse YAML content: {str(e)}")
            raise ValueError(f"Invalid YAML content: {str(e)}")


def get_app_state(fast_app: FastAPI) -> AppState:
    return fast_app.state.app_state


def add_agent_routes(fast_app: FastAPI):
    chat_model = ChatOpenAI(model=os.getenv("OPENAI_MODEL_NAME")),
    chat_prompt = ChatPromptTemplate.from_template("tell me a joke about {topic}")
    add_routes(
        app=fast_app,
        runnable=chat_prompt | chat_model[0],
        path="/joke",
    )


@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    try:
        load_env_file()
        add_agent_routes(fast_app)
        routes = [route.path for route in fast_app.router.routes]
        logging.info(f"Available routes: {routes}")

        fast_app.state.app_state = AppState()
        fast_app.state.app_state.text_splitter = get_text_splitter()
        fast_app.state.app_state.embedding_function = get_embedding_function()

        # Initialize ChromaDB for agents
        fast_app.state.app_state.agents_db = Chroma(collection_name="agents",
                                                    embedding_function=fast_app.state.app_state.embedding_function)
        # Initialize ChromaDB for ratings
        fast_app.state.app_state.ratings_db = Chroma(collection_name="ratings",
                                                     embedding_function=fast_app.state.app_state.embedding_function)

        logging.info("App state initialized successfully")
        yield
    except Exception as e:
        logging.error(f"Error during app initialization: {str(e)}")
        raise
    finally:
        logging.info("App shutdown")


app = FastAPI(lifespan=lifespan, title="Agentic DB API", description="API for managing Agentic DB",
              version="0.1.0")

# Set all CORS enabled origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Configure logging
logging.basicConfig(level=logging.ERROR, format="%(asctime)s - %(levelname)s - %(message)s")


@app.get("/")
async def root():
    logging.info("Root endpoint called")
    return {"message": "Hello World"}


@app.post("/agents")
async def add_agent(request: Request, app_state: AppState = Depends(lambda: get_app_state(app))):
    if app_state.agents_db is None or app_state.ratings_db is None:
        logging.error("Chroma DB not initialized")
        raise HTTPException(status_code=500, detail="Chroma DB not initialized")

    # Read the raw YAML content from the request body
    try:
        yaml_content_str = await request.body()
        yaml_content_str = yaml_content_str.decode('utf-8')
        logging.info("YAML content received")
    except Exception as e:
        logging.error(f"Failed to read request body: {str(e)}")
        raise HTTPException(status_code=400, detail="Failed to read request body")

    try:
        parsed_content = yaml.safe_load(yaml_content_str)
        logging.info("YAML content parsed successfully")
    except yaml.YAMLError as e:
        logging.error(f"Invalid YAML content: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid YAML content: {str(e)}")

    # Generate UUIDs for the agent and its ratings
    agent_id = str(uuid.uuid4())
    ratings_id = str(uuid.uuid4())
    parsed_content['metadata']['id'] = agent_id
    parsed_content['metadata']['ratings'] = ratings_id

    # Create the ratings manifest
    ratings_manifest = {
        "id": ratings_id,
        "agent_id": agent_id,
        "ratings": {
            "score": None  # Placeholder for the actual rating, can be set to a value between 1 and 5
        }
    }

    # Convert the updated parsed content and ratings manifest back to YAML strings
    agent_yaml_content_str = yaml.dump(parsed_content)
    ratings_yaml_content_str = yaml.dump(ratings_manifest)

    # Split the YAML content into documents
    try:
        agent_docs = app_state.text_splitter.create_documents([agent_yaml_content_str])
        ratings_docs = app_state.text_splitter.create_documents([ratings_yaml_content_str])
        logging.info("YAML content split into documents")
    except Exception as e:
        logging.error(f"Failed to split YAML content: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to split YAML content")

    try:
        # Insert documents into the respective Chroma databases
        app_state.agents_db.add_documents(agent_docs)
        app_state.ratings_db.add_documents(ratings_docs)
        logging.info("Documents added to Chroma DBs")
    except openai.RateLimitError as e:
        logging.error(f"OpenAI rate limit error: {str(e)}")
        raise HTTPException(status_code=500, detail="OpenAI rate limit error")
    except Exception as e:
        logging.error(f"Failed to add documents to Chroma DB: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to add documents to Chroma DB")

    return {
        "original_content": agent_yaml_content_str,
        "parsed_content": parsed_content,
        "agent_id": agent_id,
        "ratings_id": ratings_id,
        "ratings_manifest": ratings_manifest
    }


@app.get("/agents")
async def get_agents(query: str, app_state: AppState = Depends(lambda: get_app_state(app))):
    if app_state.agents_db is None or app_state.ratings_db is None:
        logging.error("Chroma DB not initialized")
        raise HTTPException(status_code=500, detail="Chroma DB not initialized")

    try:
        results = app_state.agents_db.similarity_search(query)
        logging.info("Similarity search query executed successfully for agents")
    except Exception as e:
        logging.error(f"Failed to execute similarity search query for agents: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to execute similarity search query for agents")

    concatenated_yaml = "---\n"
    for result in results:
        agent_content = result.page_content.strip()
        agent_data = yaml.safe_load(agent_content)
        ratings_id = agent_data['metadata'].get('ratings')

        try:
            ratings_results = app_state.ratings_db.similarity_search(ratings_id)
            logging.info(f"Similarity search query executed successfully for ratings with ID: {ratings_id}")
        except Exception as e:
            logging.error(f"Failed to execute similarity search query for ratings: {str(e)}")
            raise HTTPException(status_code=500, detail="Failed to execute similarity search query for ratings")

        if ratings_results:
            ratings_content = ratings_results[0].page_content.strip()
            ratings_data = yaml.safe_load(ratings_content)
            agent_data['ratings'] = ratings_data

        agent_yaml_content_str = yaml.dump(agent_data, default_flow_style=False)
        concatenated_yaml += agent_yaml_content_str + "\n---\n"

    # Remove the last separator if it exists
    if concatenated_yaml.endswith("\n---\n"):
        concatenated_yaml = concatenated_yaml[:-5]

    return JSONResponse(content={"agents": concatenated_yaml.strip()})


if __name__ == "__main__":
    import uvicorn

    logging.info("Starting Agentic DB API...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
    logging.info("Application shutdown")
