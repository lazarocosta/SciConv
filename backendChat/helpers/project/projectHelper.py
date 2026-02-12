import json
from datetime import datetime
import socket
import docker
import os
import config as cfg
from helpers.index import makeResponse, appendMessage


def find_files(directory):
    files = []
    directory = os.path.abspath(directory)  # Convert to absolute path for consistency

    for root, dirs, filenames in os.walk(directory):
        for filename in filenames:
            full_path = os.path.join(root, filename)

            # Remove the directory prefix from the full path
            reduced_path = os.path.relpath(full_path, directory)

            #files.append(f"./{reduced_path}")
            files.append(f"./{reduced_path}")


    return files


def read_first_50_lines(file_path):
    """Reads the first 50 lines of a file and returns them while preserving indentation."""
    lines = []
    try:
        with open(file_path, 'r', encoding='utf-8') as file:  # Specify UTF-8 encoding
            for i in range(50):
                line = file.readline()
                if not line:
                    break
                lines.append(line.rstrip())  # Preserve the indentation
    except UnicodeDecodeError:
        # If there's a UnicodeDecodeError, try reading with a different encoding
        try:
            with open(file_path, 'r', encoding='ISO-8859-1') as file:
                for i in range(50):
                    line = file.readline()
                    if not line:
                        break
                    lines.append(line.rstrip())  # Preserve the indentation
        except Exception as e:
            print(f"Error reading {file_path}: {e}")
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    return lines

def fileIsAnImage(file):
    if file.endswith('.bmp') or file.endswith('.gif') or file.endswith(
            '.jpeg') or file.endswith('.jpg') or file.endswith('.png') or file.endswith('.svg'):
        return True
    else:
        return False

def startDockerClient():
    # Seleciona uma porta disponível
    sock = socket.socket()
    sock.bind(('', 0))

    try:
        if os.path.exists('/home/ubuntu/my_docker.sock'):
            print("Using Docker socket: /home/ubuntu/my_docker.sock")
            client = docker.DockerClient(base_url='unix:///home/ubuntu/my_docker.sock')
        elif os.path.exists('/var/run/docker.sock'):
            print("Using default Docker socket: /var/run/docker.sock")
            client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
        # Check for the Docker socket used by Docker Desktop

        # # Check for the Docker socket used by Docker Desktop
        else:
            # If no specific socket is found, fall back to using environment variables
            print("No specific Docker socket found, attempting to use docker.from_env()")
            client = docker.from_env()

        # Testa a conexão
        client.ping()
        print("Conexão com Docker estabelecida com sucesso!")

    except docker.errors.DockerException as e:
        print(f"Erro ao conectar ao Docker: {str(e)}")
        raise Exception("Docker is not running")

    # Seleciona a porta e retorna o cliente Docker e a porta
    port = sock.getsockname()[1]
    print("Selected Port: " + str(port))
    return {"dockerClient": client, "port": port}

def createNetworkIfNotExists(dockerClient, networkName):
    allNetworks = dockerClient.networks.list()
    thereIsNetwork = False
    for network in allNetworks:
        if network.name == networkName:
            thereIsNetwork = True
            break
    if not thereIsNetwork:
        dockerClient.networks.create(name=networkName)


def saveDockerImage(myProjectFolder, dockerImageName, dockerTagId):
    try:
        # Check if the Docker socket exists

        if os.path.exists('/home/ubuntu/my_docker.sock'):
            print("Using default Docker socket: /home/ubuntu/my_docker.sock")
            client = docker.DockerClient(base_url='unix:///home/ubuntu/my_docker.sock')
        elif os.path.exists('/var/run/docker.sock'):
             print("Using default Docker socket: /var/run/docker.sock")
             client = docker.DockerClient(base_url='unix:///var/run/docker.sock')
        else:
            # Fallback to from_env() if no specific socket is found
            print("Default socket not found, trying docker.from_env()")
            client = docker.from_env()

        # Check if the image exists by its ID or Tag
        image = client.images.get(dockerTagId)
        print(f"Image {dockerTagId} found successfully.")

    except docker.errors.ImageNotFound:
        raise Exception(f"Image with Tag/ID {dockerTagId} not found.")
    except docker.errors.DockerException as e:
        print(f"Error connecting to Docker: {str(e)}")
        raise Exception("Docker is not running")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise Exception("An unexpected error occurred.")

    # Save the Docker image as a .tar file
    output_file = os.path.join(myProjectFolder, dockerImageName + '.tar')

    try:
        with open(output_file, 'wb') as f:
            for chunk in image.save(named=True):
                f.write(chunk)
        print(f"Image {dockerTagId} saved successfully to {output_file}")

    except Exception as e:
        print(f"Error saving the image: {str(e)}")
        raise Exception("Error saving the Docker image.")

def write_file(location, content):
    with open(location, 'w') as file:
        file.write(content)
    print(f"String saved to {location} successfully.")
    print(content)


def snapshot_directory(path):
    files_snapshot = {}
    for dirpath, dirnames, filenames in os.walk(path):
        for filename in filenames:
            filepath = os.path.join(dirpath, filename)
            stat = os.stat(filepath)
            files_snapshot[filepath] = {
                'size': stat.st_size,
                'mtime': stat.st_mtime
            }
    return files_snapshot


def process_container_diff(diff_list):
    added_files = []
    removed_files = []
    modified_files = []  # You may need additional logic to detect modifications

    for change in diff_list:
        path = change['Path']
        kind = change['Kind']

        if kind == 1:
            if path.startswith("/files"):
                added_files.append(path)
        elif kind == 2:
            removed_files.append(path)
        # For modifications, you may need to use additional logic or tools

    return added_files, removed_files, modified_files


def read_file(location):
    with open(location, 'r') as file:
        content = file.read()

    print(f"Content of {location} read successfully.")
    print(content)
    return content


def write_messagesUser_to_file(messagesToUser, projectPath):
    number = datetime.now(cfg.timezone).strftime("%y%m%d_%H%M")
    file_path = os.path.join(projectPath, f"{number}.txt")


    try:
        # Open the file in write mode
        with open(file_path, 'w') as file:
            # Iterate over the messages array
            for message in messagesToUser:
                # Extract 'role' and 'contentShort', and write them to the file in a formatted way
                file.write(f"Role: {message['role']}, ContentShort: {message['contentShort']}\n")

        print(f"Successfully written to {file_path}")
    except Exception as e:
        print(f"An error occurred: {e}")



def return_commands_to_use(requestData, messagesToUser):
    if "commandToRun" not in requestData:
        appendMessage(messagesToUser, contentShort="The commandToRun is required", stage="ParametersToUse")
        return makeResponse(messagesToUser)

    commandToRun = requestData["commandToRun"]
    # commandToUse = "make && ./iubfc 13 0.5 ./Data/IMDBID.txt ./Data/IMDBEdge.txt 10000 ./Data/dataOut.txt"
    return commandToRun

# @app.route('/project/<projectUuid>/parameters-to-use-confirmation', methods=['POST'])
# @cross_origin()
# def parameters_to_use_confirmation(projectUuid):
#     requestData = json.loads(request.data)
#     messagesToUser = []
#
#     if "messages" in requestData:
#         messagesToChat = requestData["messages"]
#         # messages= ['projects/newproject\\main.py', 'projects/newproject\\main2.py', 'projects/newproject\\main3.py', 'projects/newproject\\new\\main.py', 'projects/newproject\\new\\main2.py', 'projects/newproject\\new\\main3.py', 'projects/newproject\\new\\newnew\\main2.py', 'projects/newproject\\new\\newnew\\main3.py']
#         length = len(messagesToChat)
#         myMessage = messagesToChat[length - 1]["content"]
#     else:
#         appendMessage(messagesToUser, contentShort='Messages are missing', stage="Start")
#         return makeResponse(messagesToUser, 201, True)
#     try:
#
#         message1 = {"role": "system",
#                     "jsonObject": False,
#                     "contentShort": None,
#                     "content": "I will interact with you, and in each iteration, I will inform you of the stage name. "
#                                "In the previous phase (ProjectLocation), the user selected the project location, which is the name of a folder.\n"
#                                "The stage of this iteration is: ParametersToUse.\n"
#                                "Consider the following message and determine if the system has provided a command to run an experiment, "
#                                "or if it indicates a desire to change the information from the ProjectLocation stage.\n"
#                                "Here are your options:\n"
#                                "- Reply 'ParametersToUse' if the system has provided a command to run an experiment.\n"
#                                "- Reply 'ProjectLocation' if the system wants to change the information from the ProjectLocation stage.\n"
#                                "Message: " + myMessage}
#         messagesToUser.append(message1)
#         messagesToChat.append(message1)
#         messagesToChat = convert_json_to_string(messagesToChat)
#         # TODO descomentar
#         client = OpenAI()
#         completion = client.chat.completions.create(
#             # model="gpt-4-turbo",
#             model="gpt-4o",
#             messages=
#             messagesToChat,
#
#         )
#
#         firstMessageText = completion.choices[0].message.content
#         # messageText = '{"PL": "Python", "PLVersion": "Python 3.10",  "Dependencies": ["numpy", "matplotlib", "scikit-learn"],  "DependenciesVersion": ["numpy==1.21.5", "matplotlib==3.5.1", "scikit-learn==1.2.0"]}'
#
#         print(firstMessageText)
#
#         if firstMessageText == "ParametersToUse":
#             message1confirmation = {"role": "system",
#                                     "jsonObject": False,
#                                     "content": "The stage of this iteration is: ParametersToUse\n"
#                                                "Consider the following message and the available files on the project that you provided in a previous message. "
#                                                "They are inside the ExecutableFiles variable. Extract the command to run an experiment from the following message and "
#                                                "check that it is a valid command, taking into account the files in the project and the language syntax.\n"
#                                                'Message: ' + myMessage + '\n'
#                                                                          "Your answer follows two options:\n"
#                                                                          "- Reply 'ParametersToUse' if this message does not contain a valid command.\n"
#                                                                          "- If this message contains a valid command to be run inside a container in TTY mode, your reply should only include the command to be used in Unix-like systems.",
#                                     "contentShort": None,
#                                     }
#
#             messagesToChat.append(message1confirmation)
#             messagesToChat = convert_json_to_string(messagesToChat)
#
#             client = OpenAI()
#             completion = client.chat.completions.create(
#                 model="gpt-4-turbo",
#                 # model="gpt-4o",
#                 messages=
#                 messagesToChat,
#
#             )
#
#             messageText = completion.choices[0].message.content
#             print(messageText)
#
#             if messageText == "ParametersToUse":
#                 appendMessage(messagesToUser, contentShort="Please provide a valid command.", stage="ParametersToUse")
#             else:
#                 appendMessage(messagesToUser, content=messageText,
#                               contentShort="I will use this command to execute the experiment.\n "
#                                            "Command: " + messageText,
#                               stage="FindConfigurations")
#             return makeResponse(messagesToUser, 201, True)
#         else:
#             ####CONfirmaçao
#             message1confirmation = {"role": "system",
#                                     "jsonObject": False,
#                                     "content": 'Check if this message contains a new location for the project: "' + myMessage +
#                                                '"\nIf yes, provide the folder name. If no, respond with "NO". '
#                                                '\nPlease answer in the required format, with a one-word response.',
#                                     "contentShort": None}
#             confirmationMessage = [message1confirmation]
#             confirmationMessage = convert_json_to_string(confirmationMessage)
#
#             client = OpenAI()
#             completion = client.chat.completions.create(
#                 # model="gpt-4-turbo",
#                 model="gpt-4o",
#                 messages=
#                 confirmationMessage,
#
#             )
#
#             messageText = completion.choices[0].message.content
#             print(messageText)
#             if messageText == "NO":
#                 appendMessage(messagesToUser, contentShort="Please provide the new location of the project.",
#                               stage="ProjectLocation")
#             else:
#                 directoryPath = 'projects/' + messageText
#                 if os.path.isdir(directoryPath):
#                     print("Folder exists.")
#                     appendMessage(messagesToUser, content=messageText,
#                                   contentShort="The location of the project has been changed to " + messageText,
#                                   stage="ParametersToUse")
#                 else:
#                     print("Folder does not exist.")
#                     appendMessage(messagesToUser, contentShort="Folder does not exist.", stage="ProjectLocation")
#
#         # TODO comentar
#         # message2 = {"role": "system",
#         #             "jsonObject": False,
#         #             "contentShort": None,
#         #             "content": myMessage,
#         #             "stage": "FindConfigurations"}
#         #
#         # messagesToUser.append(message2)
#
#         return makeResponse(messagesToUser, 201, True)
#     except Exception as error:
#         print(str(error))
#         appendMessage(messagesToUser, content="Some error occurred", contentShort="Some error occurred", stage="Start")
#         return makeResponse(messagesToUser, 201, True)


# @app.route('/project/<projectUuid>/infer-files-to-run', methods=['POST'])
# @cross_origin()
# def infer_files_to_run(projectUuid):
#     directoryPath = 'projects/' + projectUuid + "/files/"
#     requestData = json.loads(request.data)
#     messagesToUser = []
#
#     if "filenames" in requestData:
#         filenames = requestData["filenames"]
#         # filenames= ['main.py', 'main2.py', 'main3.py', 'new\\main.py', 'new\\main2.py', 'new\\main3.py', 'new\\newnew\\main2.py', 'new\\newnew\\main3.py']
#     else:
#         appendMessage(messagesToUser, contentShort='Filenames are missing', stage="Start")
#         return makeResponse(messagesToUser, 201, True)
#
#     # filenames = ['main.py']
#     all_files_lines = {}
#
#     for filename in filenames:
#         if os.path.isfile(directoryPath + filename):
#             lines = read_first_50_lines(directoryPath + filename)
#             all_files_lines[filename] = lines
#         else:
#             print(f"File not found: {filename}")
#
#     # Convert the content to JSON format
#     filesContent = json.dumps(all_files_lines, indent=4)
#
#     # TODO descomentar
#
#     message1 = {"role": "system",
#                 "jsonObject": False,
#                 "contentShort": None,
#                 "content": 'Objective: Identify the main execution files within a project.'
#                            '\nDescription: The main execution files are responsible for initiating the project and typically include other modules or files. '
#                            'While these main files include other files, the reverse is not true—other files do not include the main files. Note that a project may have more than one main execution file.'
#                            '\nAction: Please provide a list of potential main execution files based on the structure of the project.'
#                 }
#
#     messagesToUser.append(message1)
#     messagesToChat = copy(message1)
#     messagesToChat["content"] = messagesToChat["content"] + str(filesContent)
#
#     client = OpenAI()
#     completion = client.chat.completions.create(
#         # model="gpt-3.5-turbo",
#         # model="gpt-4-turbo",
#         model="gpt-4o",
#         messages=[
#             messagesToChat,
#         ]
#     )
#     messageText = completion.choices[0].message.content
#     messageText = messageText.replace("```", "")
#     messageText = messageText.replace("json", "")
#
#     # TODO comentar
#     # messageText = '{"PL": "Python", "PLVersion": "Python 3.10",  ' \
#     #               '"Dependencies": ["numpy", "pandas", "matplotlib", "scipy", "shap," "tqdm"], ' \
#     #               '"DependenciesVersion": ["numpy==1.21.2", "pandas==1.3.3", "matplotlib==3.4.3", ' \
#     #               '"scipy==1.7.1", "shap==0.40.0," "tqdm==4.62.2"]}'
#     print(messageText)
#     try:
#         appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True, stage="BuildDockerFile")
#     except Exception as e:
#         message2 = {"role": "assistant",
#                     "content": messageText,
#                     "contentShort": messageText,
#                     "jsonObject": False}
#     messagesToUser.append(message2)
#
#     return makeResponse(messagesToUser, 201, True)


#
#
# @app.route('/project/<projectUuid>/find-configurations', methods=['POST'])
# @cross_origin()
# def find_configurations(projectUuid):
#     directoryPath = 'projects/' + projectUuid + "/files/"
#     requestData = json.loads(request.data)
#     messagesToUser = []
#     messagesToChat = []
#
#     if "filenames" not in requestData:
#         appendMessage(messagesToUser, contentShort='filenames are missing', stage="Start")
#         return makeResponse(messagesToUser, 201, True)
#
#     filenames = requestData["filenames"]
#     # filenames= ['main.py', 'main2.py', 'main3.py', 'new\\main.py', 'new\\main2.py', 'new\\main3.py', 'new\\newnew\\main2.py', 'new\\newnew\\main3.py']
#
#     commandToRun = return_commands_to_use(requestData, messagesToUser)
#
#     # filenames = ['main.py']
#     all_files_lines = {}
#
#     try:
#         for filename in filenames:
#             if os.path.isfile(directoryPath + filename):
#                 lines = read_first_50_lines(directoryPath + filename)
#                 all_files_lines[filename] = lines
#             else:
#                 print(f"File not found: {filename}")
#     except Exception as error:
#         appendMessage(messagesToUser, contentShort=str(error), stage="Start")
#         return makeResponse(messagesToUser, 201, True)
#
#     # Convert the content to JSON format
#     filesContent = json.dumps(all_files_lines, indent=4)
#
#     numberInteractions = 3
#     chat_message = ""
#
#     try:
#         while numberInteractions >= 0:
#             message1 = {"role": "system",
#                         "jsonObject": False,
#                         "contentShort": None,
#                         "content": chat_message + "The current stage of this interaction is: FindConfigurations"
#                                                   "\nGiven the JSON containing the name of the files, the first 50 lines of each file and the command used to execute this project, determine the following:"
#                                                   "\nThe programming language of the files."
#                                                   "\nThe version of these languages."
#                                                   "\nAny dependencies needed to execute the command and their versions."
#                                                   "\nI am providing the first 50 lines of each file. Some of the imported dependencies may not be utilized within these lines, but please return all the imported and referenced dependencies present in the files."
#                                                   '\nProvide your response in the following format:'
#                                                   '{ "PL": [all the programming languages used], "PLVersion": [all the programming language version],"Dependencies": [dependencies], "DependenciesVersion": [version of dependencies] }'
#                                                   '\nEnsure the dependency names are correct. If the provided name is incorrect, adjust it. For example, in Python, to install the sklearn dependency, the correct command is pip install scikit-learn.'
#                                                   '\nMake sure to list the most recent supported version of the programming language, and format the result in JSON.'
#                                                   "\nOnly put values that you can infer, don't put generic values"
#                                                   '\nCommand To Use: ' + commandToRun +
#                                    '\nExample response:'
#                                    '\n{ "PL": ["Python"], "PLVersion": "Python 3.8", "Dependencies": ["pandas", "tqdm"], "DependenciesVersion": ["pandas==2.2.0", "tqdm==4.62.0"]}'
#                                    '\nPlease respond in the specified format. The answer should be exactly in json format.'
#
#                         }
#
#             messagesToUser.append(message1)
#             myMessage = copy(message1)
#             myMessage["content"] = myMessage["content"] + '\nThe first 50 lines of each file: ' + str(filesContent)
#             messagesToChat.append(myMessage)
#
#             # TODO descomentar
#             client = OpenAI()
#             completion = client.chat.completions.create(
#                 model="gpt-4-turbo",
#                 # #model="gpt-4o",
#                 # model="chatgpt-4o-latest",
#                 messages=
#                 messagesToChat,
#
#             )
#             messageText = completion.choices[0].message.content
#             messageText = messageText.replace("```", "")
#             messageText = messageText.replace("json", "")
#
#             # TODO comentar
#             # messageText = '{"PL": "Python",  "PLVersion": "Python 3.10", "Dependencies": ["tqdm", "pandas", "shap","numpy", "matplotlib", "scikit-learn"],  "DependenciesVersion": ["shap==0.41.0", "numpy==1.23.4", "pandas==1.5.2", "scipy==1.9.3", "matplotlib==3.6.2", "tqdm==4.64.1"]}'
#
#             print(messageText)
#             try:
#                 appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True, stage="BuildDockerFile")
#                 return makeResponse(messagesToUser, 201, True)
#             except Exception as e:
#                 numberInteractions -= 1
#                 print("numberInteractions" + str(numberInteractions))
#                 print("Error:" + str(e))
#                 messagesToChat = []
#
#                 message1 = {"role": "system",
#                             "jsonObject": False,
#                             "contentShort": None,
#                             "content": chat_message +
#                                        "\nExtract from the following message a JSON in the required format."
#                                        "\nMessage: " + messageText +
#                                        '\nRequired JSON format: '
#                                        '{ "PL": [programming language], "PLVersion": [programming language version], "Dependencies": [dependencies], "DependenciesVersion": [version of dependencies] }'
#                                        '\nEnsure the response is in the specified JSON format. '
#                             }
#                 messagesToChat.append(message1)
#
#                 # TODO descomentar
#                 client = OpenAI()
#                 completion = client.chat.completions.create(
#                     model="gpt-4-turbo",
#                     # model="gpt-4o",
#                     # model="chatgpt-4o-latest",
#                     messages=
#                     messagesToChat,
#
#                 )
#                 messageText = completion.choices[0].message.content
#                 messageText = messageText.replace("```", "")
#                 messageText = messageText.replace("json", "")
#
#                 # TODO comentar
#                 # messageText = '{"PL": "Python",  "PLVersion": "Python 3.10", "Dependencies": ["tqdm", "pandas", "shap","numpy", "matplotlib", "scikit-learn"],  "DependenciesVersion": ["shap==0.41.0", "numpy==1.23.4", "pandas==1.5.2", "scipy==1.9.3", "matplotlib==3.6.2", "tqdm==4.64.1"]}'
#
#                 print(messageText)
#                 try:
#                     appendMessage(messagesToUser, content=json.loads(messageText), jsonObject=True,
#                                   stage="BuildDockerFile")
#                     return makeResponse(messagesToUser, 201, True)
#                 except Exception as e:
#                     print("numberInteractions" + str(numberInteractions))
#                     chat_message = "The previous result is incorrect. I encountered this error: " + str(e) + \
#                                    "\nPlease consider the following information.\n"
#
#     except Exception as error:
#         appendMessage(messagesToUser, content="I got this error:" + str(error),
#                       contentShort="I got this error:" + str(error), stage="Start")
#         return makeResponse(messagesToUser, 201, True)
#
#     appendMessage(messagesToUser, content="Some error occurred", contentShort="Some error occurred", stage="Start")
#     return makeResponse(messagesToUser, 201, True)
#
