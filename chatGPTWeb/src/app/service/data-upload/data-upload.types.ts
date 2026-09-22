// src/app/service/data-upload/data-upload.types.ts

export enum DataUploadStages {
    Idle            = 'Idle',
    AnalysingData   = 'AnalysingData',   // file uploaded, GPT inferring metadata
    ReviewMetadata  = 'ReviewMetadata',  // user reviewing/editing metadata
    Publishing      = 'Publishing',      // background Zenodo upload running
    Completed       = 'Completed',
    Error           = 'Error',
}

export interface DataUploadState {
    stage: DataUploadStages;
    isLoading: boolean;
    uploadUuid?: string;
    totalBytes?: number;
    zenodoMetadata?: any;
    zenodoTemplate?: any;
    progressText?: string;
    dois?: string[];
    errorMessage?: string;
}
