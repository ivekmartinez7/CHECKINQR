const qrImage = document.getElementById('qr-code');

if (qrImage) {
    const status = document.getElementById('qr-state');
    const button = document.getElementById('qr-refresh');
    const baseUrl = qrImage.getAttribute('src');
    let timer;
    let revision = 0;

    function refreshQr() {
        if (document.hidden) return;
        clearTimeout(timer);
        const request = ++revision;
        const next = new Image();
        button.disabled = true;

        function finish(ready) {
            if (request !== revision) return;
            revision++;
            clearTimeout(timer);
            qrImage.style.visibility = ready ? 'visible' : 'hidden';
            status.textContent = ready ? 'QR listo para escanear' : 'QR sin conexión. Intenta actualizarlo.';
            if (ready) qrImage.src = next.src;
            button.disabled = false;
            timer = setTimeout(refreshQr, ready ? 9000 : 3000);
        }

        next.onload = () => finish(true);
        next.onerror = () => finish(false);
        next.src = baseUrl + '?t=' + Date.now();
        timer = setTimeout(() => finish(false), 4000);
    }

    button.hidden = false;
    button.addEventListener('click', refreshQr);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            revision++;
            clearTimeout(timer);
            qrImage.style.visibility = 'hidden';
        } else {
            refreshQr();
        }
    });
    refreshQr();
}
