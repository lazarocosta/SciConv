// src/app/service/repro-workflow/repro-workflow.service.ts
import { Injectable } from '@angular/core';
import { BehaviorSubject, Subject } from 'rxjs';

import { BackendService } from '../backend.service'; // <- ajusta se o teu ficheiro tiver outro nome/caminho
import { Message } from '../../interface/interfaces';
import { ReproState, ReproStages } from './repro-workflow.types';

@Injectable({ providedIn: 'root' })
export class ReproWorkflowService {
    private readonly initialState: ReproState = {
        stage: ReproStages.ProjectLocation,
        role: 'system',
        messages: [],
        projectUuid: '',
        commandToRun: '',
        outputSpec: '',
        runProgressDetail: '',
        executableFiles: undefined,
        configurationFiles: {},
        configurations: undefined,
        dockerImageID: undefined,
        logs: undefined,
        added_files: undefined,
        removed_files: undefined,
        modified_files: undefined,
        messageToAsk: undefined,
        examplesToAsk: undefined,
        stageAfterChat: undefined,
        isLoading: false,
        errorMessage: undefined,
        GO_BACK_NUMBER: 3,
        goBack: 3,
        artifactZenodoDoi: undefined,
        artifactIsUploading: false,
        datasetMetadataDraft: undefined,
        datasetMetadataTemplate: undefined,
        datasetMetadataReady: false,
        artifactMetadataDraft: undefined,
        artifactMetadataReady: false,
    };

    private readonly stateSubject = new BehaviorSubject<ReproState>(this.initialState);
    readonly state$ = this.stateSubject.asObservable();

    /** Emits when GPT triggers a full reset — component should call goToAppStart(true). */
    private readonly resetRequestedSubject = new Subject<void>();
    readonly resetRequested$ = this.resetRequestedSubject.asObservable();

    private get state(): ReproState {
        return this.stateSubject.value;
    }

    constructor(private backend: BackendService) {}

    private patch(p: Partial<ReproState>) {
        this.stateSubject.next({ ...this.state, ...p });
    }

    private pushMessages(...msgs: Message[]) {
        this.patch({ messages: [...this.state.messages, ...msgs] });
    }

    changeStage(newStage: ReproStages) {
        this.patch({ stage: newStage });
        this.performActionBasedOnStage(newStage);
    }

    // Chamado pelo HomeComponent quando o user envia texto
    sendUserMessage(userMessage: string) {
        this.pushMessages({
            role: this.state.role,
            contentShort: userMessage,
            content: userMessage,
            jsonObject: false,
        });

        switch (this.state.stage) {
            case ReproStages.ProjectLocation:
                this.patch({ projectUuid: userMessage });
                this.changeStage(ReproStages.FindProjectFiles);
                break;

            case ReproStages.WaitForDataInput:
                this._handleDataInputText(userMessage);
                break;

            case ReproStages.ParametersToUse:
                this.parametersToUseConfirmation();
                break;

            case ReproStages.SpecifyOutputs:
                this.specifyOutputsConfirmation();
                break;

            case ReproStages.WaitChatInteraction:
                this.chatInteraction();
                break;

            case ReproStages.FindConfigurationsInteraction:
                this.findConfigurationsFunc(userMessage);
                break;

            case ReproStages.Completed:
                this.changeStage(ReproStages.ResearchArtifact);
                break;

            default:
                break;
        }
    }

    // Chamado pelo HomeComponent no submit do upload
    uploadProject(formData: FormData) {
        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.uploadProject(formData).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });

                const len = response.length;
                const last = response[len - 1];

                // The FindProjectFiles message always carries the projectUuid
                const uuidMsg = response.find((m: any) => m.stage === ReproStages.FindProjectFiles);
                if (uuidMsg) this.patch({ projectUuid: uuidMsg.content });

                const hasWaitForData = response.some((m: any) => m.stage === ReproStages.WaitForDataInput);

                if (hasWaitForData) {
                    // Push all messages (question visible, uuid message has null contentShort so hidden)
                    this.pushMessages(...response);
                    this.patch({ stage: ReproStages.WaitForDataInput });
                } else if (last.stage === ReproStages.FindProjectFiles) {
                    // Combined zip or no-data-needed path — push any preceding messages
                    if (response.length > 1) this.pushMessages(...response.slice(0, -1));
                    this.changeStage(ReproStages.InferDatasetMetadata);
                } else if (last.stage) {
                    this.pushMessages(...response);
                    this.changeStage(last.stage);
                }
            },
            error: (err: any) => {
                console.error('[UploadProject] HTTP', err?.status, err?.error);
                this.patch({
                    isLoading: false,
                    errorMessage: `Upload failed (HTTP ${err?.status ?? 'network error'}). Please check your connection or authentication.`,
                });
            }
        });
    }

    private performActionBasedOnStage(stage: ReproStages) {
        switch (stage) {
            case ReproStages.ProjectLocation:
                this.projectLocation();
                break;
            case ReproStages.WaitForDataInput:
                this.pushMessages({
                    role: 'assistant',
                    content: 'No problem. How would you like to provide your dataset?',
                    contentShort: 'No problem. How would you like to provide your dataset?',
                    jsonObject: false,
                });
                break;
            case ReproStages.InferDatasetMetadata:
                this.inferDatasetMetadata();
                break;
            case ReproStages.ExternalizeData:
                this.externalizeData();
                break;
            case ReproStages.FindProjectFiles:
                this.findProjectFiles();
                break;
            case ReproStages.ParametersToUse:
                this.parametersToUseFunc();
                break;
            case ReproStages.SpecifyOutputs:
                this.specifyOutputsFunc();
                break;
            case ReproStages.FindConfigurations:
                this.findConfigurations();
                break;
            case ReproStages.FindConfigurationsInteraction:
                this.findConfigurationsInteraction();
                break;
            case ReproStages.WaitChatInteraction:
                this.waitChatInteraction(this.state.messageToAsk);
                break;
            case ReproStages.BuildDockerFile:
                this.buildDockerFile();
                break;
            case ReproStages.BuildDockerImage:
                this.buildDockerImage();
                break;
            case ReproStages.RunContainer:
                this.runContainer();
                break;
            case ReproStages.RunFailed:
                this.pushMessages({
                    role: 'assistant',
                    content: 'What would you like to do?',
                    contentShort: 'What would you like to do?',
                    jsonObject: false,
                });
                break;
            case ReproStages.ResearchArtifact:
                this.researchArtifact();
                break;
            case ReproStages.Completed:
                this.pushMessages({
                    role: 'assistant',
                    content: 'All done! Your experiment has been packaged into a zip file. You can download it directly or upload it to Zenodo to get a citable DOI — both options are available below.',
                    contentShort: 'All done! Your experiment has been packaged into a zip file. You can download it directly or upload it to Zenodo to get a citable DOI — both options are available below.',
                    jsonObject: false,
                });
                break;
        }
    }




    // -----------------------------------------------------------------------
    // WaitForDataInput helpers
    // -----------------------------------------------------------------------

    private _handleDataInputText(text: string) {
        // Send raw text to backend — GPT classifies intent (no_data / doi / unclear)
        this.patch({ isLoading: true });
        this.backend.provideData(this.state.projectUuid, { text }).subscribe({
            next: (response: any) => this._handleProvideDataResponse(response),
            error: () => this.patch({ isLoading: false, errorMessage: 'Could not process your response. Please check your connection and try again.' }),
        });
    }

    provideNoData() {
        this.patch({ isLoading: true });
        this.backend.provideData(this.state.projectUuid, { no_data: true }).subscribe({
            next: (response: any) => this._handleProvideDataResponse(response),
            error: () => this.patch({ isLoading: false, errorMessage: 'Could not process your response. Please check your connection and try again.' }),
        });
    }

    provideDataDoi(doi: string) {
        this.patch({ isLoading: true });
        this.backend.provideData(this.state.projectUuid, { dataset_doi: doi }).subscribe({
            next: (response: any) => this._handleProvideDataResponse(response),
            error: () => this.patch({ isLoading: false, errorMessage: 'Could not resolve the Zenodo DOI. Please check the URL and try again.' }),
        });
    }

    provideDataFile(file: File) {
        this.patch({ isLoading: true });
        const fd = new FormData();
        fd.append('data_file', file);
        this.backend.provideData(this.state.projectUuid, fd).subscribe({
            next: (response: any) => this._handleProvideDataResponse(response),
            error: () => this.patch({ isLoading: false, errorMessage: 'Failed to upload your data file. Please check the file and try again.' }),
        });
    }

    private _handleProvideDataResponse(response: any[]) {
        this.patch({ isLoading: false });
        this.pushMessages(...response);
        const last = response[response.length - 1];
        if (last?.stage === ReproStages.FindProjectFiles) {
            this.changeStage(ReproStages.FindProjectFiles);
        } else if (last?.stage === ReproStages.InferDatasetMetadata) {
            this.changeStage(ReproStages.InferDatasetMetadata);
        } else if (last?.stage === ReproStages.WaitForDataInput) {
            this.patch({ stage: ReproStages.WaitForDataInput });
        }
    }

    // -----------------------------------------------------------------------

    private projectLocation() {
        this.pushMessages({
            role: "assistant",
            content: "Please provide the location of the project.",
            contentShort: "Please provide the location of the project.",
            jsonObject: false,
            examples:
                "Examples:\n" +
                "The root folder of the project is located at example_folder_name\n" +
                "example_folder_name"
        });
    }

    private inferDatasetMetadata() {
        this.patch({ isLoading: true, errorMessage: undefined, datasetMetadataReady: false });

        this.pushMessages({
            role: 'assistant',
            content: 'Analysing dataset to infer Zenodo metadata...',
            contentShort: 'Analysing dataset to infer Zenodo metadata...',
            jsonObject: false,
        });

        this.backend.inferDatasetMetadata(this.state.projectUuid).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                if (response.skip) {
                    // Strategy doesn't need Zenodo upload — proceed automatically
                    this.changeStage(ReproStages.ExternalizeData);
                    return;
                }
                const metadataList = response.zenodo_metadata || [];
                const draft = metadataList.length > 0 ? metadataList[0].metadata : {};
                this.patch({
                    datasetMetadataDraft: draft,
                    datasetMetadataTemplate: response.template,
                    datasetMetadataReady: true,
                });
            },
            error: () => {
                // On inference failure, skip metadata and proceed
                this.patch({ isLoading: false });
                this.changeStage(ReproStages.ExternalizeData);
            }
        });
    }

    confirmDatasetMetadata(metadata: any) {
        this.patch({ datasetMetadataReady: false, isLoading: true, errorMessage: undefined });

        this.pushMessages({
            role: 'assistant',
            content: 'Uploading your dataset to Zenodo. The upload may take some time, depending on the dataset size. I\'m doing my best to make it fast.',
            contentShort: 'Uploading dataset to Zenodo... The upload may take some time, depending on the dataset size. I\'m doing my best to make it fast.',
            jsonObject: false,
        });

        this.backend.externalizeData(this.state.projectUuid, metadata).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);
                this.changeStage(ReproStages.FindProjectFiles);
            },
            error: (err: any) => {
                const status = err?.status ?? 'network error';
                const body = err?.error;
                const detail = Array.isArray(body)
                    ? body.map((m: any) => m.contentShort || m.content).join(' | ')
                    : (typeof body === 'string' ? body : JSON.stringify(body ?? {}));
                this.patch({
                    isLoading: false,
                    errorMessage: `Dataset upload failed (HTTP ${status}): ${detail}`,
                });
            }
        });
    }

    skipDatasetMetadata() {
        this.patch({ datasetMetadataReady: false });
        this.changeStage(ReproStages.ExternalizeData);
    }

    private externalizeData() {
        this.patch({ isLoading: true, errorMessage: undefined });

        this.pushMessages({
            role: 'assistant',
            content: 'Uploading your dataset to Zenodo. The upload may take some time, depending on the dataset size. I\'m doing my best to make it fast.',
            contentShort: 'Uploading dataset to Zenodo... The upload may take some time, depending on the dataset size. I\'m doing my best to make it fast.',
            jsonObject: false,
        });

        this.backend.externalizeData(this.state.projectUuid).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);
                this.changeStage(ReproStages.FindProjectFiles);
            },
            error: (err: any) => {
                const status = err?.status ?? 'network error';
                const body = err?.error;
                const detail = Array.isArray(body)
                    ? body.map((m: any) => m.contentShort || m.content).join(' | ')
                    : (typeof body === 'string' ? body : JSON.stringify(body ?? {}));
                console.error('[ExternalizeData] HTTP', status, detail);
                this.patch({
                    isLoading: false,
                    errorMessage: `Dataset upload failed (HTTP ${status}): ${detail}`,
                });
            }
        });
    }

    private findProjectFiles() {
        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.findProjectFiles(this.state.projectUuid).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const len = response.length;
                const last = response[len - 1];

                if (last.stage === ReproStages.ParametersToUse) {
                    const { ExecutableFiles, ConfigurationFiles, ProjectUuid } = last.content;
                    this.patch({
                        executableFiles: ExecutableFiles,
                        configurationFiles: ConfigurationFiles,
                        projectUuid: ProjectUuid,
                    });
                }

                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to find project files. Please check your connection or authentication.",
                });
            }
        });
    }

    private parametersToUseFunc() {
        const msg =
            'Please provide the command to run the experiment.\n\n' +
            '**Examples:**\n' +
            '  python ./main.py\n' +
            '  python ./myfile.py && cd folder && python ./myfile2.py';
        this.pushMessages({
            role: 'assistant',
            content: msg,
            contentShort: msg,
            jsonObject: false,
        });
    }

    private parametersToUseConfirmation() {
        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.parametersToUseConfirmation(this.state.projectUuid, this.state.messages).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const len = response.length;
                const last = response[len - 1];

                if (last.stage === ReproStages.SpecifyOutputs) {
                    // Clear stale configurations so re-detection always runs on the new command.
                    this.patch({ commandToRun: last.content, configurations: undefined });
                    // If we're re-running after a failure, skip SpecifyOutputs and go straight to run
                    if (this.state.pendingRerun) {
                        this.patch({ pendingRerun: false });
                        this.changeStage(ReproStages.RunContainer);
                        return;
                    }
                }
                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to confirm parameters. Please check your input or authentication.",
                });
            }
        });
    }

    private specifyOutputsFunc() {
        this.patch({ isLoading: true });
        this.backend.inferOutputFolder(this.state.projectUuid).subscribe({
            next: (res) => {
                this.patch({ isLoading: false });
                const folder = res?.folder;
                const examplesBlock =
                    '\n\nExamples:\n' +
                    '  yes\n' +
                    '  output/statistics.json output/summary.csv output/report.txt\n' +
                    '  results/\n' +
                    '  output/*.csv';
                if (folder) {
                    const parts = folder.trim().split(/\s+/);
                    const hasFolder = parts.some(p => p.endsWith('/') || !p.includes('.'));
                    const detectedLine = parts.every(p => p.endsWith('/') || !p.includes('.'))
                        ? `I detected ${parts.length > 1 ? 'output folders' : 'an output folder'} from your code:\n\`${folder}\``
                        : `I detected these output paths from your code:\n\`${folder}\``;
                    const folderNote = hasFolder
                        ? '\n\n⚠️ When a folder is specified (e.g. `output/`), every file inside it will be captured as output. ' +
                          'If you only want specific files, list them by name instead (e.g. `output/results.csv output/plot.png`).'
                        : '';
                    const msg =
                        `${detectedLine}\n\n` +
                        'Does this look correct?\n' +
                        '• Type **yes** to confirm\n' +
                        '• List specific files to replace (space-separated)\n' +
                        '• Add more paths to the existing list\n' +
                        '• Click **Skip** if your experiment produces no output files' +
                        folderNote +
                        examplesBlock;
                    this.pushMessages({
                        role: 'assistant',
                        content: msg,
                        contentShort: msg,
                        jsonObject: false,
                    });
                } else {
                    const msg =
                        'Which files or folders does your experiment write as output?\n\n' +
                        '• List specific files: `output/results.csv output/plot.png`\n' +
                        '• Or specify a folder: `results/` — every file inside will be captured\n' +
                        '• You can mix both: `output/summary.csv plots/`\n' +
                        '• Click **Skip** if your experiment produces no output files' +
                        examplesBlock;
                    this.pushMessages({
                        role: 'assistant',
                        content: msg,
                        contentShort: msg,
                        jsonObject: false,
                    });
                }
            },
            error: () => {
                this.patch({ isLoading: false });
                this.pushMessages({
                    role: 'assistant',
                    content: 'Please specify the output files or directories your experiment creates.',
                    contentShort: 'Please specify the output files or directories your experiment creates.',
                    jsonObject: false,
                });
            }
        });
    }

    private specifyOutputsConfirmation() {
        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.specifyOutputs(this.state.projectUuid, this.state.messages).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const len = response.length;
                const last = response[len - 1];

                if (last.stage === ReproStages.FindConfigurations) {
                    this.patch({ outputSpec: last.content });
                    // If re-running after a failure, skip FindConfigurations and go straight to run
                    if (this.state.pendingRerun) {
                        this.patch({ pendingRerun: false });
                        this.changeStage(ReproStages.RunContainer);
                        return;
                    }
                }
                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: 'Failed to save output specification.',
                });
            }
        });
    }

    skipOutputSpec() {
        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.skipOutputs(this.state.projectUuid).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const last = response[response.length - 1];
                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: 'Failed to skip output specification.',
                });
            }
        });
    }

    uploadArtifact() {
        // Step 1: infer metadata via GPT, show editor before uploading
        this.patch({ artifactIsUploading: true, errorMessage: undefined, artifactMetadataReady: false });

        this.backend.inferArtifactMetadata(this.state.projectUuid).subscribe({
            next: (res: any) => {
                this.patch({
                    artifactIsUploading: false,
                    artifactMetadataDraft: {
                        title: res.title || '',
                        description: res.description || '',
                        creator_name: '',
                    },
                    artifactMetadataReady: true,
                    stage: ReproStages.InferArtifactMetadata,
                });
            },
            error: () => {
                // Fall back to uploading with generic metadata
                this.patch({ artifactIsUploading: false });
                this._doUploadArtifact({});
            }
        });
    }

    confirmArtifactMetadata(draft: { title: string; description: string; creator_name: string }) {
        this._doUploadArtifact(draft);
    }

    skipArtifactMetadata() {
        this._doUploadArtifact({});
    }

    private _doUploadArtifact(body: any) {
        this.patch({ artifactIsUploading: true, errorMessage: undefined, stage: ReproStages.Completed });

        this.backend.uploadArtifactToZenodo(this.state.projectUuid, body).subscribe({
            next: (response: any) => {
                this.patch({ artifactIsUploading: false });
                this.pushMessages(...response);
                const last = response[response.length - 1];
                const doi = last?.content?.doi || last?.contentShort?.match(/DOI: (.+)/)?.[1];
                if (doi) this.patch({ artifactZenodoDoi: doi });
            },
            error: () => {
                this.patch({
                    artifactIsUploading: false,
                    errorMessage: 'Failed to upload artifact to Zenodo.',
                });
            }
        });
    }

    private findConfigurations() {
        this.pushMessages({
            role: "assistant",
            content: "",
            contentShort: "Detecting your experiment's dependencies...",
            jsonObject: false
        });

        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.findConfigurations(this.state.projectUuid, this.state.executableFiles, this.state.commandToRun).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const len = response.length;
                const last = response[len - 1];

                this.patch({ configurations: last.content });
                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to infer environment configuration. Please try again.",
                });
            }
        });
    }

    private _formatConfigurations(configs: any): string {
        if (!configs) return '(not yet detected)';
        if (typeof configs !== 'object') return String(configs);
        const lines: string[] = [];

        // Language + version (Python-style keys or R-style PL/PLVersion keys)
        const lang: string[] = configs.PL ?? (configs.python ? ['Python'] : []);
        const plv = configs.PLVersion;
        const version: string = (Array.isArray(plv) ? plv[0] : plv) ?? configs.python ?? '';
        if (lang.length) lines.push(`• Language: ${lang.join(', ')}${version ? ' — ' + version : ''}`);

        // Packages (handles both 'Dependencies' and 'packages'/'dependencies' keys)
        const packages: string[] = configs.Dependencies ?? configs.packages ?? configs.dependencies ?? [];
        if (packages.length) {
            lines.push('• Packages:');
            packages.forEach((p: string) => lines.push(`    - ${p}`));
        }

        const system: string[] = configs.system ?? configs.system_dependencies ?? [];
        if (system.length) {
            lines.push('• System dependencies:');
            system.forEach((s: string) => lines.push(`    - ${s}`));
        }

        // Fallback for any unrecognised keys
        const handled = new Set(['PL', 'PLVersion', 'Dependencies', 'python', 'packages', 'dependencies', 'system', 'system_dependencies']);
        Object.entries(configs).forEach(([k, v]) => {
            if (!handled.has(k)) lines.push(`• ${k}: ${JSON.stringify(v)}`);
        });
        return lines.length ? lines.join('\n') : JSON.stringify(configs);
    }

    private findConfigurationsInteraction() {
        // If configurations haven't been detected yet, run detection first.
        // This can happen when navigating directly to this stage via universal chat.
        if (!this.state.configurations) {
            this.findConfigurations();
            return;
        }
        const formatted = this._formatConfigurations(this.state.configurations);
        this.pushMessages({
            role: "assistant",
            content: `Here's the environment I'll build for your experiment:\n\n${formatted}\n\nDoes this look correct? You can ask me to change anything.`,
            contentShort: `Here's the environment I'll build:\n\n${formatted}\n\nDoes this look correct?`,
            jsonObject: false,
            examples:
                "Yes, looks good.\n" +
                "Change Python to 3.9.\n" +
                "Change pandas to 2.2.2.\n" +
                "Remove pandas.\n" +
                "Add scipy 1.11 and scikit-learn 1.3."
        });
    }

    private findConfigurationsFunc(userMessage: string) {
        this.patch({ isLoading: true, errorMessage: undefined });

        const myMessage =
            "Here are the configuration used: " + JSON.stringify(this.state.configurations) +
            "\nThe question is: Are they correct, or would you like to change anything?\n" +
            "The user action is: " + userMessage;

        this.backend.findConfigurationsFunc(this.state.projectUuid, this.state.messages, myMessage).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });

                const len = response.length;
                const last = response[len - 1];

                // Config was updated: store the new config and jump straight back to
                // FindConfigurationsInteraction so the user gets one confirmation step,
                // not two (avoids the WaitChatInteraction detour).
                if (last.stage === ReproStages.WaitChatInteraction && last.jsonObject === true) {
                    this.patch({ configurations: last.content });
                    this.changeStage(ReproStages.FindConfigurationsInteraction);
                    return;
                }

                // For plain navigation tokens ("BuildDockerFile", "WaitChatInteraction")
                // the backend now sends contentShort=null — skip pushing them to the chat.
                const visibleMessages = response.filter((m: any) => m.contentShort !== null);
                if (visibleMessages.length) this.pushMessages(...visibleMessages);

                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to process configuration changes. Please try again.",
                });
            }
        });
    }

    private buildDockerFile() {

        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.BuildDockerFile(this.state.projectUuid, this.state.messages).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const last = response[response.length - 1];
                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to build Dockerfile. Please try again.",
                });
            }
        });
    }

    private buildDockerImage() {
        this.pushMessages({
            role: "assistant",
            content: "",
            contentShort: "Setting up your experiment environment. This may take a few minutes...",
            jsonObject: false
        });

        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.BuildDockerImage(this.state.projectUuid, this.state.messages).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const last = response[response.length - 1];

                if (last.goBack) {
                    const newGoBack = this.state.goBack - 1;

                    if (newGoBack <= 0) {
                        this.patch({ goBack: this.state.GO_BACK_NUMBER });
                        this.changeStage(ReproStages.FindConfigurationsInteraction);
                        return;
                    }
                    this.patch({ goBack: newGoBack });
                    this.auxFunction(response);
                    return;
                }

                this.auxFunction(response);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to build Docker image. Please verify the Dockerfile or environment.",
                });
            }
        });
    }

    private auxFunction(response: any) {
        const last = response[response.length - 1];

        if (last.stage === ReproStages.RunContainer) {
            this.patch({ dockerImageID: last.content });
        } else if (last.stage === ReproStages.WaitChatInteraction) {
            this.patch({ messageToAsk: undefined, stageAfterChat: ReproStages.FindConfigurations });
        }

        if (last.stage) this.changeStage(last.stage);
    }

    private runContainer() {
        this.pushMessages({
            role: "assistant",
            content: "",
            contentShort: "Running your experiment...",
            jsonObject: false
        });

        this.patch({ isLoading: true, errorMessage: undefined, runProgressDetail: '' });

        // Poll run_progress.json every 2 s while the request is in-flight
        const pollId = window.setInterval(() => {
            this.backend.getRunProgress(this.state.projectUuid).subscribe({
                next: (p: any) => {
                    if (p?.detail) {
                        this.patch({ runProgressDetail: p.detail });
                    }
                }
            });
        }, 2000);

        this.backend.RunContainer(
            this.state.projectUuid,
            this.state.dockerImageID,
            this.state.commandToRun,
            this.state.messages
        ).subscribe({
            next: (response: any) => {
                window.clearInterval(pollId);
                this.patch({ isLoading: false, runProgressDetail: '' });
                this.pushMessages(...response);

                const last = response[response.length - 1];

                if (!last.stage) {
                    const { logs, added_files, removed_files, modified_files } = last.content;
                    const outputFiles: string[] = last.output_files ?? [];

                    const noFilesNote = outputFiles.length === 0
                        ? '\n\nNo output files were captured.' : '';

                    this.patch({
                        logs,
                        added_files,
                        removed_files,
                        modified_files,
                        outputFiles,
                        messageToAsk:
                            `Your experiment ran successfully!${noFilesNote}\n\n` +
                            'Does the result look correct? If something is wrong, describe what happened and I\'ll help fix it.',
                        stageAfterChat: ReproStages.ResearchArtifact
                    });

                    this.changeStage(ReproStages.WaitChatInteraction);
                } else {
                    this.patch({ messageToAsk: undefined });
                    this.changeStage(last.stage);
                }
            },
            error: () => {
                window.clearInterval(pollId);
                this.patch({
                    isLoading: false,
                    runProgressDetail: '',
                    errorMessage: "Failed to run container. Please review the command or image and try again.",
                });
            }
        });
    }

    private researchArtifact() {
        this.pushMessages({
            role: "assistant",
            content: "",
            contentShort: "Packaging your research artifact...",
            jsonObject: false
        });

        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.ResearchArtifact(
            this.state.projectUuid,
            this.state.dockerImageID,
            this.state.commandToRun,
            this.state.messages
        ).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const last = response[response.length - 1];
                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Failed to generate the research artifact. Please try again.",
                });
            }
        });
    }

    private waitChatInteraction(messageToAsk?: string) {
        if (!messageToAsk) return;

        const msg: Message = this.state.examplesToAsk
            ? {
                role: "assistant",
                content: messageToAsk,
                contentShort: messageToAsk,
                jsonObject: false,
                examples: this.state.examplesToAsk
            }
            : {
                role: "assistant",
                content: messageToAsk,
                contentShort: messageToAsk,
                jsonObject: false
            };

        this.pushMessages(msg);
        this.patch({ examplesToAsk: undefined });
    }

    private chatInteraction() {
        const nextStep = this.state.stageAfterChat ?? ReproStages.FindConfigurationsInteraction;

        this.patch({ isLoading: true, errorMessage: undefined });

        this.backend.ChatInteraction(this.state.projectUuid, this.state.messages, nextStep).subscribe({
            next: (response: any) => {
                this.patch({ isLoading: false });
                this.pushMessages(...response);

                const last = response[response.length - 1];

                if (last.stage === ReproStages.WaitChatInteraction) {
                    this.patch({
                        messageToAsk: "What might have caused this unexpected result?\n",
                        examplesToAsk:
                            "I want to change the execution parameters.\n" +
                            "I want to change the project location.\n" +
                            "I want to change the computing environment used.\n"
                    });
                }

                if (last.stage) this.changeStage(last.stage);
            },
            error: () => {
                this.patch({
                    isLoading: false,
                    errorMessage: "Chat interaction failed. Please try again.",
                });
            }
        });
    }

    // -----------------------------------------------------------------------
    // Run failure recovery
    // -----------------------------------------------------------------------

    /** Fix only the run command — reuses existing Docker image, skips rebuild */
    fixRunCommand() {
        this.patch({ pendingRerun: true });
        this.pushMessages({
            role: 'system',
            contentShort: 'Fix the run command',
            content: 'Fix the run command',
            jsonObject: false,
        });
        this.changeStage(ReproStages.ParametersToUse);
    }

    /** Fix dependencies — rebuilds Dockerfile and Docker image, then reruns */
    fixDependencies() {
        this.pushMessages({
            role: 'system',
            contentShort: 'Fix dependencies / Dockerfile',
            content: 'Fix dependencies / Dockerfile',
            jsonObject: false,
        });
        this.changeStage(ReproStages.BuildDockerFile);
    }

    /** Fix output spec — re-specifies output files, then reruns without rebuild */
    fixOutputSpec() {
        this.patch({ pendingRerun: true });
        this.pushMessages({
            role: 'system',
            contentShort: 'Fix output specification',
            content: 'Fix output specification',
            jsonObject: false,
        });
        this.changeStage(ReproStages.SpecifyOutputs);
    }

    /** Retry the same command with the same image (for transient errors) */
    retryRun() {
        this.pushMessages({
            role: 'system',
            contentShort: 'Retry',
            content: 'Retry',
            jsonObject: false,
        });
        this.changeStage(ReproStages.RunContainer);
    }

    /** True while any background process is running — chat input should be disabled */
    get isBusy(): boolean {
        return this.state.isLoading || this.state.artifactIsUploading;
    }

    /**
     * Universal context-aware message handler.
     * Replaces the hard-coded sendUserMessage switch for the Repro flow.
     * GPT classifies intent and we route accordingly.
     */
    sendUniversalMessage(userMessage: string) {
        if (this.isBusy) return;

        // Push the user's message into the conversation
        this.pushMessages({
            role: 'user',
            contentShort: userMessage,
            content: userMessage,
            jsonObject: false,
        });

        // Stages with deterministic handlers — bypass universal chat entirely
        if (this.state.stage === ReproStages.WaitForDataInput) {
            this._handleDataInputText(userMessage);
            return;
        }

        this.patch({ isLoading: true, errorMessage: undefined });

        // Send last 6 messages so GPT sees what was already said to the user
        const recentMessages = this.state.messages.slice(-6).map(m => ({
            role: m.role === 'user' ? 'user' : 'assistant',
            content: m.contentShort ?? m.content ?? '',
        }));

        this.backend.universalChat(
            this.state.projectUuid,
            userMessage,
            this.state.stage,
            recentMessages
        ).subscribe({
            next: (res) => {
                this.patch({ isLoading: false });

                // Show GPT's reply, except when confirming automatic stages where
                // the system's own progress messages make it redundant.
                const silentConfirmStages = [
                    ReproStages.FindConfigurationsInteraction,
                    ReproStages.WaitChatInteraction,
                ];
                const isSilentConfirm = res.action === 'confirm' &&
                    silentConfirmStages.includes(this.state.stage);
                const isSilentSetCommand = res.action === 'set_command' &&
                    this.state.stage === ReproStages.ParametersToUse;
                const isSilentSetOutputs = res.action === 'set_outputs' &&
                    this.state.stage === ReproStages.SpecifyOutputs;
                if (res.reply && !isSilentConfirm && !isSilentSetCommand && !isSilentSetOutputs) {
                    this.pushMessages({
                        role: 'assistant',
                        content: res.reply,
                        contentShort: res.reply,
                        jsonObject: false,
                    });
                }

                // Only act when GPT attached an explicit action tag
                switch (res.action) {
                    case 'navigate':
                        if (res.target_stage) {
                            this.changeStage(res.target_stage as ReproStages);
                        }
                        break;

                    case 'confirm':
                        this._confirmCurrentStage();
                        break;

                    case 'set_command':
                        this.parametersToUseConfirmation();
                        break;

                    case 'set_outputs':
                        this.specifyOutputsConfirmation();
                        break;

                    case 'update_config':
                        this.findConfigurationsFunc(userMessage);
                        break;

                    case 'report_issue':
                        this.changeStage(ReproStages.RunFailed);
                        break;

                    case 'reset':
                        this.reset();
                        this.resetRequestedSubject.next();
                        break;

                    // null / undefined — GPT replied but took no action (ambiguous input, question, etc.)
                }

                // Stage-specific fallbacks when GPT replied but attached no action tag.
                // These stages have deterministic user intent — a reply without an action
                // almost always means GPT intended to confirm but forgot the tag.
                if (!res.action && res.reply) {
                    if (this.state.stage === ReproStages.SpecifyOutputs) {
                        this.specifyOutputsConfirmation();
                    } else if (this.state.stage === ReproStages.ParametersToUse) {
                        this.parametersToUseConfirmation();
                    }
                }
            },
            error: () => {
                this.patch({ isLoading: false, errorMessage: 'Chat failed. Please try again.' });
            }
        });
    }

    private _confirmCurrentStage() {
        switch (this.state.stage) {
            case ReproStages.ParametersToUse:
                this.parametersToUseConfirmation();
                break;
            case ReproStages.SpecifyOutputs:
                this.specifyOutputsConfirmation();
                break;
            case ReproStages.FindConfigurationsInteraction:
                this.findConfigurationsFunc('yes');
                break;
            case ReproStages.WaitChatInteraction:
                // If stageAfterChat is set (e.g. BuildDockerFile after config update),
                // honour it. Otherwise default to packaging the artifact.
                const nextAfterChat = this.state.stageAfterChat ?? ReproStages.ResearchArtifact;
                this.patch({ stageAfterChat: undefined });
                this.changeStage(nextAfterChat);
                break;
        }
    }

    reset() {
        this.stateSubject.next(this.initialState);
    }
}
