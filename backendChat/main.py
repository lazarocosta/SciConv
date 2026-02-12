import os

import requests
from urllib.parse import quote
from flask import Flask
from flask_cors import CORS, cross_origin
from flasgger import Swagger

import config as cfg
from routes.project import project_bp
from routes.survey import survey_bp
from routes.article import article_bp

from helpers.project.projectHelper import startDockerClient
from helpers.index import makeResponse

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)
swagger = Swagger(app)
@app.route("/", methods=['GET'])
@cross_origin()
def home():
    messagesToUser = [
        {"role": "assistant",
         "contentShort": "HOME PAGE",
         "content": "HOME PAGE",
         "jsonObject": False}
    ]
    return makeResponse(messagesToUser, 201, True)


# Registar blueprints
app.register_blueprint(project_bp)
app.register_blueprint(survey_bp)
app.register_blueprint(article_bp)


if __name__ == '__main__':
    try:
        # definir HOST_VOLUME_PATH global no config
        cfg.HOST_VOLUME_PATH = os.environ.get("HOST_VOLUME_PATH", "")
        if cfg.HOST_VOLUME_PATH == "":
            raise Exception("HOST_VOLUME_PATH is None")
        print(f"The HOST_VOLUME_PATH is '{cfg.HOST_VOLUME_PATH}'.")

        dockerClientResult = startDockerClient()

        # criar pastas
        if not os.path.exists(cfg.PROJECTS_LOCATION):
            os.makedirs(cfg.PROJECTS_LOCATION)
            print(f"Folder '{cfg.PROJECTS_LOCATION}' created.")
        else:
            print(f"Folder '{cfg.PROJECTS_LOCATION}' already exists.")

        if not os.path.exists(cfg.QUESTIONNAIRES_LOCATION):
            os.makedirs(cfg.QUESTIONNAIRES_LOCATION)
            print(f"Folder '{cfg.QUESTIONNAIRES_LOCATION}' created.")
        else:
            print(f"Folder '{cfg.QUESTIONNAIRES_LOCATION}' already exists.")

        app.run(host='0.0.0.0', port=8081)
    except Exception as e:
        print(str(e))