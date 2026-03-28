document.addEventListener("DOMContentLoaded", () => {
    const statusClassMap = {
        waiting: "chip chip-waiting",
        in_consultation: "chip chip-active",
        served: "chip chip-served",
    };

    const forms = document.querySelectorAll("form[data-loading-form]");
    forms.forEach((form) => {
        form.addEventListener("submit", () => {
            const submitButton = form.querySelector('button[type="submit"]');
            if (!submitButton || submitButton.disabled) {
                return;
            }

            submitButton.dataset.originalText = submitButton.innerHTML;
            submitButton.disabled = true;
            submitButton.innerHTML = submitButton.dataset.loadingText || "Please wait...";
        });
    });

    window.addEventListener("pageshow", () => {
        document.querySelectorAll('button[type="submit"][data-original-text]').forEach((button) => {
            button.disabled = false;
            button.innerHTML = button.dataset.originalText;
        });
    });

    const queueShell = document.querySelector("[data-queue-poll-url]");
    if (!queueShell) {
        return;
    }

    const pollUrl = queueShell.dataset.queuePollUrl;
    const statusChip = queueShell.querySelector('[data-queue-field="status_label"]');
    const statusText = queueShell.querySelector('[data-queue-field="status_text"]');
    const nowServing = queueShell.querySelector('[data-queue-field="now_serving"]');
    const patientsAhead = queueShell.querySelector('[data-queue-field="patients_ahead"]');
    const estimatedWait = queueShell.querySelector('[data-queue-field="estimated_wait"]');

    const updateQueue = async () => {
        try {
            const response = await fetch(pollUrl, {
                headers: {
                    "X-Requested-With": "XMLHttpRequest",
                },
            });

            if (!response.ok) {
                return;
            }

            const data = await response.json();

            if (statusChip) {
                statusChip.className = statusClassMap[data.entry_status] || "chip chip-neutral";
                statusChip.textContent = data.status_label;
            }

            if (statusText) {
                statusText.textContent = data.status_label;
            }

            if (nowServing) {
                nowServing.textContent = data.now_serving ?? "-";
            }

            if (patientsAhead) {
                patientsAhead.textContent = data.patients_ahead;
            }

            if (estimatedWait) {
                estimatedWait.textContent = data.estimated_wait !== null
                    ? `${data.estimated_wait} mins`
                    : "-";
            }
        } catch (error) {
            console.error("Queue refresh failed.", error);
        }
    };

    window.setInterval(updateQueue, 8000);
});
