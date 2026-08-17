type AndroidDownloadBridge = {
  saveImage(dataUrl: string, fileName: string): void;
};

declare global {
  interface Window {
    MYAndroidDownloads?: AndroidDownloadBridge;
  }
}

function blobToDataUrl(blob: Blob) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read image data"));
    reader.onload = () => {
      if (typeof reader.result === "string") {
        resolve(reader.result);
      } else {
        reject(new Error("Failed to read image data"));
      }
    };
    reader.readAsDataURL(blob);
  });
}

export async function downloadImageBlob(blob: Blob, fileName: string) {
  const nativeBridge = window.MYAndroidDownloads;
  if (nativeBridge?.saveImage) {
    nativeBridge.saveImage(await blobToDataUrl(blob), fileName);
    return;
  }

  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function downloadImageSource(source: string, fileName: string) {
  const response = await fetch(source);
  if (!response.ok) {
    throw new Error(`Image download failed: HTTP ${response.status}`);
  }
  await downloadImageBlob(await response.blob(), fileName);
}
