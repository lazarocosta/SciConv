// src/app/service/repro-workflow/repro-workflow.types.ts
import { Message } from "../../interface/interfaces";
import {ZenodoMetadataView} from "../dataset-analysis/dataset-analysis.types";

export enum ReproStages {
    ProjectLocation = 'ProjectLocation',
    WaitForDataInput = 'WaitForDataInput',
    InferDatasetMetadata = 'InferDatasetMetadata',
    ExternalizeData = 'ExternalizeData',
    FindProjectFiles = 'FindProjectFiles',
    ParametersToUse = 'ParametersToUse',
    SpecifyOutputs = 'SpecifyOutputs',
    FindConfigurations = 'FindConfigurations',
    FindConfigurationsInteraction = 'FindConfigurationsInteraction',
    BuildDockerFile = 'BuildDockerFile',
    BuildDockerImage = 'BuildDockerImage',
    RunContainer = 'RunContainer',
    RunFailed = 'RunFailed',
    ResearchArtifact = 'ResearchArtifact',
    WaitChatInteraction = 'WaitChatInteraction',
    InferArtifactMetadata = 'InferArtifactMetadata',
    Completed = 'Completed'
}

export interface ReproState {
    stage: ReproStages;
    role: string;
    messages: Message[];
    projectUuid: string;
    commandToRun: string;
    outputSpec: string;
    runProgressDetail: string;
    executableFiles: any;
    configurationFiles: any;
    configurations: any;
    dockerImageID: any;
    logs: any;
    added_files: any;
    removed_files: any;
    modified_files: any;
    messageToAsk?: string;
    examplesToAsk?: string;
    stageAfterChat?: ReproStages;
    isLoading: boolean;
    errorMessage?: string;
    goBack: number;
    readonly GO_BACK_NUMBER: number;
    // Output files from run-container
    outputFiles?: string[];
    // Recovery flag: skip rebuild steps and jump straight to RunContainer
    pendingRerun?: boolean;
    // Artifact upload to Zenodo
    artifactZenodoDoi?: string;
    artifactIsUploading: boolean;
    // Dataset metadata inference
    datasetMetadataDraft?: any;
    datasetMetadataTemplate?: any;
    datasetMetadataReady: boolean;
    // Artifact metadata inference
    artifactMetadataDraft?: { title: string; description: string; creator_name: string };
    artifactMetadataReady: boolean;
}

export interface ReproFromDoiState {
    phase: 'idle' | 'init' | 'running' | 'completed' | 'error';
    newProjectUuid?: string;
    runProgressDetail?: string;
    logs?: string;
    outputFiles?: string[];
    commandToRun?: string;
    dataStrategy?: string;
    errorMessage?: string;
}