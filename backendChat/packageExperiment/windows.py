def writeWindowsFIle(rootPath, projectUuid, commands, dockerTagId, hasDatabase, port,
                     manifest=None):
    """
    Generate runExperiment.bat batch scripts for the research artifact.

    manifest (optional): the reproducibility_manifest dict. When provided,
    data provisioning (Step 5) and a /data volume mount (Step 6) are added
    before the docker run command.
    """
    indexCommand = 0
    arrayFiles = []
    windowsFileLocation = "runExperiment"

    data_strategy = (manifest or {}).get("data_strategy", "no_data")
    needs_data = data_strategy not in ("no_data", "embed")
    is_rclone_strategy = data_strategy in ("externalize_files", "external_doi")
    doi = ""
    if is_rclone_strategy:
        doi = ((manifest or {}).get("dataset") or {}).get("doi", "")

    for command in commands:
        if indexCommand != 0:
            myWindowsFileLocation = windowsFileLocation + "_" + str(indexCommand) + ".bat"
        else:
            myWindowsFileLocation = windowsFileLocation + ".bat"

        file = open(rootPath + myWindowsFileLocation, "w", newline='')
        arrayFiles.append(myWindowsFileLocation)

        file.write("@ECHO OFF\n")
        file.write("@ECHO Script is running...\n")
        file.write("set date=%DATE:/=-%\n")
        file.write("set hrs=%time:~0,2%\n")
        file.write("set mns=%time:~3,2%\n")
        file.write("set scs=%time:~6,2%\n")
        file.write("set time=%hrs%-%mns%-%scs%\n")
        file.write("set time=%date%_%time: =%\n")
        file.write("ECHO %time%\n")
        file.write("set execution=execution_%time%\n")
        file.write("@ECHO Docker Image is loading...\n")
        file.write("docker load --input " + projectUuid + ".tar\n")

        # ------------------------------------------------------------------
        # Step 5: Data Provisioning
        # ------------------------------------------------------------------
        if needs_data:
            file.write("\nREM Step 5: Data Provisioning\n")
            file.write("@ECHO Provisioning dataset...\n")

            if is_rclone_strategy:
                file.write("IF NOT EXIST data\\ mkdir data\n")
                # Auto-install rclone locally if not present (no admin rights needed)
                file.write("where rclone >nul 2>&1\n")
                file.write("IF %ERRORLEVEL% NEQ 0 (\n")
                file.write("    @ECHO rclone not found - attempting auto-install...\n")
                file.write("    IF NOT EXIST rclone_bin mkdir rclone_bin\n")
                file.write("    powershell -Command \"try { Invoke-WebRequest -Uri"
                           " 'https://downloads.rclone.org/rclone-current-windows-amd64.zip'"
                           " -OutFile 'rclone_bin\\rclone.zip' -UseBasicParsing } catch { exit 1 }\"\n")
                file.write("    IF %ERRORLEVEL% EQU 0 (\n")
                file.write("        powershell -Command \"Expand-Archive -Path 'rclone_bin\\rclone.zip'"
                           " -DestinationPath 'rclone_bin' -Force\"\n")
                file.write("        FOR /R rclone_bin %%F IN (rclone.exe) DO SET PATH=%%~dpF;%PATH%\n")
                file.write("        @ECHO rclone installed locally.\n")
                file.write("    ) ELSE (\n")
                file.write("        @ECHO rclone auto-install failed.\n")
                file.write("    )\n")
                file.write(")\n")
                # Mount with rclone if available, otherwise fall back to Python download
                file.write("where rclone >nul 2>&1\n")
                file.write("IF %ERRORLEVEL% EQU 0 (\n")
                file.write("    SET RCLONE_CONF=%TEMP%\\rclone_dataset.conf\n")
                file.write("    (echo [dataset]& echo type = doi& echo doi = " + doi + ") > %RCLONE_CONF%\n")
                file.write("    rclone mount dataset: data --config %RCLONE_CONF%"
                           " --read-only --daemon --allow-non-empty --no-modtime\n")
                file.write("    timeout /t 3 /nobreak > nul\n")
                file.write("    dir /b data 2>nul | findstr . >nul\n")
                file.write("    IF %ERRORLEVEL% EQU 0 (\n")
                file.write("        del %RCLONE_CONF%\n")
                file.write("        SET RCLONE_MOUNTED=1\n")
                file.write("    ) ELSE (\n")
                file.write("        @ECHO rclone mount empty or failed - falling back to rclone copy/download...\n")
                file.write("        del %RCLONE_CONF% 2>nul\n")
                file.write("        python provision_data.py --manifest reproducibility_manifest.json --output data\n")
                file.write("        IF %ERRORLEVEL% NEQ 0 (\n")
                file.write("            @ECHO Data provisioning FAILED. Halting.\n")
                file.write("            EXIT /B 1\n")
                file.write("        )\n")
                file.write("        SET RCLONE_MOUNTED=0\n")
                file.write("    )\n")
                file.write(") ELSE (\n")
                file.write("    @ECHO rclone unavailable - downloading via provision_data.py...\n")
                file.write("    python provision_data.py --manifest reproducibility_manifest.json --output data\n")
                file.write("    IF %ERRORLEVEL% NEQ 0 (\n")
                file.write("        @ECHO Data provisioning FAILED. Halting.\n")
                file.write("        EXIT /B 1\n")
                file.write("    )\n")
                file.write("    SET RCLONE_MOUNTED=0\n")
                file.write(")\n")
            else:
                # externalize / chunk_and_externalize / external_doi: download
                file.write("python provision_data.py --manifest reproducibility_manifest.json --output data\n")
                file.write("IF %ERRORLEVEL% NEQ 0 (\n")
                file.write("    @ECHO Data provisioning FAILED. Halting.\n")
                file.write("    EXIT /B 1\n")
                file.write(")\n")

        # ------------------------------------------------------------------
        # Step 6: Docker run (with optional /data volume mount)
        # ------------------------------------------------------------------
        file.write("\n@ECHO Docker Container is running...\n")
        file.write(
            "docker network ls|Findstr " + projectUuid +
            " >nul 2>&1 || docker network create " + projectUuid + "\n"
        )

        string = "docker run -it --name " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        if hasDatabase:
            string += " --network=" + projectUuid
        if port is not None:
            string += " -p " + port + ":" + port

        string += " -v " + projectUuid + ":/files"

        # Mount provisioned data as read-only /data volume (Step 6)
        if needs_data:
            string += " -v %CD%\\data:/data:ro"

        safe_cmd = command.replace('"', '\\"')
        string += ' ' + dockerTagId + ' /bin/sh -c "' + safe_cmd + '"'
        file.write(string + "\n")

        file.write("@ECHO Copying the content of the Container to %execution%\n")
        string = "docker cp " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        string += ":/files ./%execution%"
        file.write(string + "\n")

        file.write("@ECHO Script ended\n")
        file.write("@ECHO Stopping the docker Container...\n")
        string = "docker stop " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        file.write(string + "\n")

        file.write("@ECHO Removing the docker Container...\n")
        string = "start docker rm " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        file.write(string + "\n")

        # Unmount rclone if it was used
        if is_rclone_strategy:
            file.write("\nREM Unmount rclone if it was used\n")
            file.write("IF \"%RCLONE_MOUNTED%\"==\"1\" rclone rc mount/unmountall\n")

        file.write("@ECHO End!!!\n")
        file.write("PAUSE\n")
        file.close()

        indexCommand += 1

    return arrayFiles
