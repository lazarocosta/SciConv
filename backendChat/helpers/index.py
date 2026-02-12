import json
import os
from typing import List, Dict, Any

import flask
from openai import OpenAI

def makeResponse(response=None, status=200, isJson=True):
    """
    Returns a Flask Response.
    If isJson=True, ensures the response body is JSON serialized.
    """
    if response is None:
        response = {}

    if isJson:
        mimetype = "application/json"
        # Only json.dumps if response is not already a string/bytes
        if not isinstance(response, (str, bytes)):
            response = json.dumps(response)
    else:
        mimetype = flask.Response.default_mimetype

    return flask.Response(response=response, status=status, mimetype=mimetype)

def appendMessage(messages, content, contentShort=None, stage=None, role="assistant",
                  jsonObject=False, examples=None, goBack=None):
    if contentShort is None:
        contentShort = content

    message = {
        "role": role,
        "jsonObject": jsonObject,
        "contentShort": contentShort,
        "content": content
    }

    if stage is not None:
        message["stage"] = stage
    if examples is not None:
        message["examples"] = examples
    if goBack is not None:
        message["goBack"] = goBack

    messages.append(message)

def return_messages(requestData, messagesToUser):
    if "messages" not in requestData:
        appendMessage(messagesToUser, content="I can’t find the messages", stage="Start")
        return makeResponse(messagesToUser, status=200, isJson=True)

    return requestData["messages"]


#def callGPTModel(messagesToChat, modelUsed="gpt-4.1", temperature=0.2):
def callGPTModel(messagesToChat, modelUsed="o4-mini"):

    """
    Calls OpenAI Chat Completions API.
    messagesToChat must be a list of dicts with 'role' and 'content'.
    """
    if not isinstance(messagesToChat, list):
        raise ValueError("messagesToChat must be a list.")

    messagesToChat = convert_json_to_string(messagesToChat)

    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise EnvironmentError("Missing OPENAI_API_KEY in environment or .env file.")

    client = OpenAI(api_key=openai_api_key)

    completion = client.chat.completions.create(
        model=modelUsed,
        messages=messagesToChat,
        #temperature=temperature,
    )

    result = completion.choices[0].message.content
    print(result)
    return result


def _extract_json_safe(text: str):
    """
    Cleans GPT output and extracts valid JSON even if GPT includes
    quotes, markdown fences, or extra explanation.
    """
    cleaned = (text or "").strip()

    cleaned = cleaned.replace("```json", "").replace("```", "")

    if cleaned.startswith('"') and cleaned.endswith('"'):
        cleaned = cleaned[1:-1].strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start:end + 1]

    try:
        return json.loads(cleaned)
    except Exception:
        return None


def convert_json_to_string(input_list):
    """
    Ensures every message['content'] is a string (as required by OpenAI API).
    Keeps only role/content keys for OpenAI.
    """
    converted_list = []

    for message in input_list:
        role = message.get("role")
        content = message.get("content")

        if role is None:
            raise ValueError("Each message must have a 'role' field.")

        if isinstance(content, str):
            converted_content = content
        else:
            try:
                converted_content = json.dumps(content)
            except (TypeError, ValueError):
                converted_content = str(content)

        converted_list.append({
            "role": role,
            "content": converted_content
        })

    return converted_list