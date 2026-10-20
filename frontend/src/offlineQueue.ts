export type QueuedDecision = {
  id: string;
  planId: string;
  decision: "approved" | "rejected";
  note: string;
  createdAt: string;
};

const DB_NAME = "purva-drishti";
const STORE = "approval-actions";

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => request.result.createObjectStore(STORE, { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function enqueueDecision(action: QueuedDecision): Promise<void> {
  const db = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const request = db.transaction(STORE, "readwrite").objectStore(STORE).put(action);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
  db.close();
}

export async function getQueuedDecisions(): Promise<QueuedDecision[]> {
  const db = await openDatabase();
  const items = await new Promise<QueuedDecision[]>((resolve, reject) => {
    const request = db.transaction(STORE, "readonly").objectStore(STORE).getAll();
    request.onsuccess = () => resolve(request.result as QueuedDecision[]);
    request.onerror = () => reject(request.error);
  });
  db.close();
  return items;
}

export async function removeQueuedDecision(id: string): Promise<void> {
  const db = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const request = db.transaction(STORE, "readwrite").objectStore(STORE).delete(id);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  });
  db.close();
}
