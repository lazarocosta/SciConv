// src/app/service/data-upload/data-upload.service.ts

import { Injectable } from '@angular/core';
import { BehaviorSubject } from 'rxjs';
import { BackendService } from '../backend.service';
import { DataUploadStages, DataUploadState } from './data-upload.types';

const INITIAL_STATE: DataUploadState = {
    stage: DataUploadStages.Idle,
    isLoading: false,
};

@Injectable({ providedIn: 'root' })
export class DataUploadService {

    private _state = new BehaviorSubject<DataUploadState>({ ...INITIAL_STATE });
    readonly state$ = this._state.asObservable();

    private _pollId?: number;

    constructor(private backend: BackendService) {}

    get state(): DataUploadState {
        return this._state.getValue();
    }

    private patch(partial: Partial<DataUploadState>) {
        this._state.next({ ...this.state, ...partial });
    }

    reset() {
        clearInterval(this._pollId);
        this._state.next({ ...INITIAL_STATE });
    }

    // ── Step 1: upload file, GPT infers Zenodo metadata ──────────────────────

    uploadFile(file: File) {
        const formData = new FormData();
        formData.append('data_file', file);

        this.patch({
            stage: DataUploadStages.AnalysingData,
            isLoading: true,
            errorMessage: undefined,
        });

        this.backend.uploadDataDirect(formData).subscribe({
            next: (res: any) => {
                // zenodo_metadata comes back as [{metadata: {...}}] — unwrap to flat object
                const raw = res.zenodo_metadata;
                const flatMetadata = Array.isArray(raw) && raw.length > 0
                    ? (raw[0]?.metadata ?? raw[0])
                    : (raw ?? {});

                this.patch({
                    stage: DataUploadStages.ReviewMetadata,
                    isLoading: false,
                    uploadUuid: res.uuid,
                    totalBytes: res.total_bytes,
                    zenodoMetadata: flatMetadata,
                    zenodoTemplate: res.template,
                });
            },
            error: (err: any) => {
                this.patch({
                    stage: DataUploadStages.Error,
                    isLoading: false,
                    errorMessage: err?.error?.error || 'Upload failed. Please try again.',
                });
            },
        });
    }

    // ── Step 2: user confirmed metadata — kick off background upload ─────────

    confirmUpload(metadata: any) {
        const uuid = this.state.uploadUuid;
        if (!uuid) return;

        this.patch({
            stage: DataUploadStages.Publishing,
            isLoading: true,
            progressText: 'Starting…',
            errorMessage: undefined,
        });

        this.backend.confirmDataUpload(uuid, { metadata }).subscribe({
            next: () => this._startPolling(uuid),
            error: (err: any) => {
                this.patch({
                    stage: DataUploadStages.Error,
                    isLoading: false,
                    errorMessage: err?.error?.error || 'Could not start upload. Please try again.',
                });
            },
        });
    }

    // ── Polling ───────────────────────────────────────────────────────────────

    private _startPolling(uuid: string) {
        clearInterval(this._pollId);
        this._pollId = window.setInterval(() => {
            this.backend.getDataUploadProgress(uuid).subscribe({
                next: (res: any) => {
                    const status   = res?.status   ?? '';
                    const progress = res?.progress ?? '';
                    const dois     = res?.dois     ?? [];

                    if (status === 'completed') {
                        clearInterval(this._pollId);
                        this.patch({
                            stage: DataUploadStages.Completed,
                            isLoading: false,
                            progressText: progress,
                            dois,
                        });
                    } else if (status === 'error') {
                        clearInterval(this._pollId);
                        this.patch({
                            stage: DataUploadStages.Error,
                            isLoading: false,
                            errorMessage: progress || 'Upload to Zenodo failed.',
                        });
                    } else {
                        // still running — update progress text only
                        this.patch({ progressText: progress });
                    }
                },
            });
        }, 2500);
    }
}
