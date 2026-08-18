import test from 'node:test';
import assert from 'node:assert/strict';

import {
    getApiBaseURL,
    getBackendHint,
    getWritingStreamURL,
} from '../src/config/api.ts';
import { bootstrapApp } from '../src/app/bootstrap.ts';
import {
    readSharedTopic,
    writeSharedTopic,
} from '../src/stores/sharedTopic.ts';

test('bootstrapApp mounts immediately without waiting for cleanup', () => {
    let mounted = false;
    let cleanupCalled = false;

    bootstrapApp({
        mountApp: () => {
            mounted = true;
        },
        clearPapers: () => {
            cleanupCalled = true;
            return new Promise<void>(() => {});
        },
    });

    assert.equal(mounted, true);
    assert.equal(cleanupCalled, true);
});

test('bootstrapApp swallows cleanup rejection', async () => {
    let mounted = false;

    bootstrapApp({
        mountApp: () => {
            mounted = true;
        },
        clearPapers: async () => {
            throw new Error('backend offline');
        },
    });

    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(mounted, true);
});

test('api config defaults align with uvicorn default port', () => {
    assert.equal(getApiBaseURL(undefined), '/api');
    assert.equal(getBackendHint(undefined), 'http://127.0.0.1:8000');
});

test('api config respects explicit backend origin', () => {
    assert.equal(getApiBaseURL('http://127.0.0.1:8000'), 'http://127.0.0.1:8000/api');
    assert.equal(getBackendHint('http://127.0.0.1:8000'), 'http://127.0.0.1:8000');
});

test('writing stream URL uses the shared API base config', () => {
    assert.equal(getWritingStreamURL(undefined), '/api/writing/generate-stream');
    assert.equal(
        getWritingStreamURL('http://127.0.0.1:8000'),
        'http://127.0.0.1:8000/api/writing/generate-stream',
    );
});

test('shared topic persists retrieval topic for writing page defaults', () => {
    const store = new Map<string, string>();
    const sessionStorageMock = {
        getItem(key: string) {
            return store.has(key) ? store.get(key)! : null;
        },
        setItem(key: string, value: string) {
            store.set(key, value);
        },
        removeItem(key: string) {
            store.delete(key);
        },
    };

    Object.defineProperty(globalThis, 'sessionStorage', {
        value: sessionStorageMock,
        configurable: true,
        writable: true,
    });

    writeSharedTopic('  无人机协同配送应急物资  ');

    assert.equal(readSharedTopic(), '无人机协同配送应急物资');
    assert.equal(
        sessionStorageMock.getItem('lit_review_topic'),
        '无人机协同配送应急物资',
    );
});

