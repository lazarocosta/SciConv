import os


def writeLinuxFile(rootPath, projectUuid, commands, dockerTagId, hasDatabase, port,
                   manifest=None):
    """
    Generate runExperiment.sh shell scripts for the research artifact.

    manifest (optional): the reproducibility_manifest dict. When provided,
    data provisioning (Step 5) and a /data volume mount (Step 6) are added
    before the docker run command.
    """
    indexCommand = 0
    arrayFiles = []
    LinuxFileLocation = "runExperiment"

    data_strategy = (manifest or {}).get("data_strategy", "no_data")
    needs_data = data_strategy not in ("no_data", "embed")
    # externalize_files and external_doi both have individual files on Zenodo
    # and can use the rclone doi backend for zero-copy mount
    is_rclone_strategy = data_strategy in ("externalize_files", "external_doi")
    doi = ""
    if is_rclone_strategy:
        doi = ((manifest or {}).get("dataset") or {}).get("doi", "")

    for command in commands:
        if indexCommand != 0:
            myLinuxFileLocation = LinuxFileLocation + "_" + str(indexCommand) + ".sh"
        else:
            myLinuxFileLocation = LinuxFileLocation + ".sh"

        file = open(rootPath + myLinuxFileLocation, "w", newline='')
        arrayFiles.append(myLinuxFileLocation)

        file.write("#!/bin/bash\n")
        file.write("echo 'Script is running...'\n")
        file.write("time=`date +%d-%m-%Y_%H:%M:%S`\n")
        file.write("echo $time\n")
        file.write('execution="execution_$time"\n')
        file.write("echo 'Docker Image is loading...'\n")
        file.write("docker load --input " + projectUuid + ".tar\n")

        # ------------------------------------------------------------------
        # Step 5: Data Provisioning
        # ------------------------------------------------------------------
        if needs_data:
            file.write("\n# Step 5: Data Provisioning\n")
            file.write("echo 'Provisioning dataset...'\n")

            if is_rclone_strategy:
                file.write("mkdir -p ./data\n")
                # Auto-install rclone locally if not present (no sudo needed)
                file.write("if ! command -v rclone &>/dev/null; then\n")
                file.write("    echo 'rclone not found — attempting auto-install...'\n")
                file.write("    mkdir -p ./rclone_bin\n")
                file.write("    RCLONE_ARCH=amd64\n")
                file.write("    case $(uname -m) in aarch64|arm64) RCLONE_ARCH=arm64 ;; esac\n")
                file.write("    if curl -fsSL \"https://downloads.rclone.org/rclone-current-linux-${RCLONE_ARCH}.zip\""
                           " -o ./rclone_bin/rclone.zip 2>/dev/null; then\n")
                file.write("        (cd ./rclone_bin && unzip -q rclone.zip)\n")
                file.write("        RCLONE_BIN=$(find ./rclone_bin -name 'rclone' -type f 2>/dev/null | head -1)\n")
                file.write("        if [ -n \"$RCLONE_BIN\" ]; then\n")
                file.write("            chmod +x \"$RCLONE_BIN\"\n")
                file.write("            export PATH=\"$(dirname $(realpath \"$RCLONE_BIN\")):$PATH\"\n")
                file.write("            echo 'rclone installed locally.'\n")
                file.write("        fi\n")
                file.write("    else\n")
                file.write("        echo 'rclone auto-install failed (no curl or network issue).'\n")
                file.write("    fi\n")
                file.write("fi\n")
                # Mount with rclone if available, otherwise fall back to Python download
                file.write("if command -v rclone &>/dev/null; then\n")
                file.write("    RCLONE_CONF=$(mktemp /tmp/rclone_XXXXXX.conf)\n")
                file.write("    printf '[dataset]\\ntype = doi\\ndoi = " + doi + "\\n' > $RCLONE_CONF\n")
                file.write("    rclone mount dataset: ./data \\\n")
                file.write("        --config $RCLONE_CONF \\\n")
                file.write("        --read-only --daemon --allow-non-empty --no-modtime\n")
                file.write("    sleep 3\n")
                file.write("    if [ -n \"$(ls -A ./data 2>/dev/null)\" ]; then\n")
                file.write("        rm -f $RCLONE_CONF\n")
                file.write("        RCLONE_MOUNTED=1\n")
                file.write("    else\n")
                file.write("        echo 'rclone mount empty or failed — falling back to rclone copy/download...'\n")
                file.write("        fusermount -u ./data 2>/dev/null || true\n")
                file.write("        rm -f $RCLONE_CONF\n")
                file.write("        python3 provision_data.py --manifest reproducibility_manifest.json --output ./data\n")
                file.write("        if [ $? -ne 0 ]; then\n")
                file.write("            echo 'Data provisioning FAILED. Halting.'\n")
                file.write("            exit 1\n")
                file.write("        fi\n")
                file.write("        RCLONE_MOUNTED=0\n")
                file.write("    fi\n")
                file.write("else\n")
                file.write("    echo 'rclone unavailable — downloading via provision_data.py...'\n")
                file.write("    python3 provision_data.py --manifest reproducibility_manifest.json --output ./data\n")
                file.write("    if [ $? -ne 0 ]; then\n")
                file.write("        echo 'Data provisioning FAILED. Halting.'\n")
                file.write("        exit 1\n")
                file.write("    fi\n")
                file.write("    RCLONE_MOUNTED=0\n")
                file.write("fi\n")
            else:
                # externalize / chunk_and_externalize / external_doi: download
                file.write("python3 provision_data.py --manifest reproducibility_manifest.json --output ./data\n")
                file.write("if [ $? -ne 0 ]; then\n")
                file.write("    echo 'Data provisioning FAILED. Halting.'\n")
                file.write("    exit 1\n")
                file.write("fi\n")

        # ------------------------------------------------------------------
        # Step 6: Docker run (with optional /data volume mount)
        # ------------------------------------------------------------------
        file.write("\necho 'Docker Container is running...'\n")
        file.write(
            "docker network ls|grep " + projectUuid +
            " > /dev/null || docker network create " + projectUuid + "\n"
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
            string += " -v $(pwd)/data:/data:ro"

        safe_cmd = command.replace('"', '\\"')
        string += ' ' + dockerTagId + ' /bin/sh -c "' + safe_cmd + '"'
        file.write(string + "\n")

        file.write("echo 'Copying the content of the Container to' $execution\n")
        string = "docker cp " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        string += ":/files ./$execution"
        file.write(string + "\n")

        file.write("echo 'Script ended'\n")
        file.write("echo 'Stopping the docker Container...'\n")
        string = "docker stop " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        file.write(string + "\n")

        file.write("echo 'Removing the docker Container...'\n")
        string = "docker rm " + projectUuid
        if indexCommand != 0:
            string += "_" + str(indexCommand)
        file.write(string + "\n")

        # Unmount rclone if it was used
        if is_rclone_strategy:
            file.write("\n# Unmount rclone if it was used\n")
            file.write('if [ "${RCLONE_MOUNTED:-0}" = "1" ]; then\n')
            file.write("    rclone rc mount/unmountall\n")
            file.write("fi\n")

        file.write("echo 'End!!!'\n")
        file.write('read -p "Press ENTER to close" x\n')
        file.close()
        os.chmod(rootPath + myLinuxFileLocation, 0o777)

        indexCommand += 1

    return arrayFiles
