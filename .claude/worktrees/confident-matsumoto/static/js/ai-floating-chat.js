(function() {
    const chatRoot = document.getElementById('ai-floating-chat');
    if (!chatRoot) {
        return;
    }

    const STORAGE_PREFIX = 'ai_chat_';
    const STORAGE_KEYS = {
        position: STORAGE_PREFIX + 'position',
        open: STORAGE_PREFIX + 'open',
        model: STORAGE_PREFIX + 'model'
    };
    const MAX_HISTORY = 10;

    const toggleButton = chatRoot.querySelector('.ai-chat-toggle');
    const closeButton = chatRoot.querySelector('.ai-chat-close');
    const header = chatRoot.querySelector('.ai-chat-header');
    const messagesEl = chatRoot.querySelector('#ai-chat-messages');
    const inputEl = chatRoot.querySelector('#ai-chat-input');
    const sendButton = chatRoot.querySelector('#ai-chat-send');
    const modelSelect = chatRoot.querySelector('#ai-chat-model');
    const isAuthenticated = chatRoot.dataset.authenticated === '1';

    let chatHistory = [];
    let dragState = null;
    let skipToggle = false;

    function isMobileView() {
        return window.matchMedia('(max-width: 767.98px)').matches;
    }

    function readStorage(key, fallback) {
        try {
            const raw = localStorage.getItem(key);
            return raw ? JSON.parse(raw) : fallback;
        } catch (error) {
            return fallback;
        }
    }

    function writeStorage(key, value) {
        try {
            localStorage.setItem(key, JSON.stringify(value));
        } catch (error) {
            return;
        }
    }

    function normalizeHistory(data) {
        if (!Array.isArray(data)) {
            return [];
        }
        return data.filter(item => item && typeof item.content === 'string' && item.role)
            .slice(-MAX_HISTORY);
    }

    function renderHistory() {
        messagesEl.innerHTML = '';
        chatHistory.forEach(item => {
            messagesEl.appendChild(createMessageElement(item.role, item.content));
        });
        scrollToBottom();
    }

    function addMessage(role, content, options) {
        const message = { role, content, ts: Date.now() };
        if (!options || !options.skipStore) {
            chatHistory.push(message);
            chatHistory = normalizeHistory(chatHistory);
            renderHistory();
            return;
        }
        const node = createMessageElement(role, content, options);
        messagesEl.appendChild(node);
        scrollToBottom();
        return node;
    }

    function createMessageElement(role, content, options) {
        const wrapper = document.createElement('div');
        wrapper.className = 'ai-chat-message ai-chat-message--' + role;
        if (options && options.loading) {
            wrapper.classList.add('is-loading');
        }

        const bubble = document.createElement('div');
        bubble.className = 'ai-chat-bubble';
        bubble.textContent = content;
        wrapper.appendChild(bubble);
        return wrapper;
    }

    function scrollToBottom() {
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    function setOpen(open) {
        chatRoot.classList.toggle('open', open);
        chatRoot.classList.toggle('collapsed', !open);
        writeStorage(STORAGE_KEYS.open, open ? 1 : 0);
        if (open && inputEl) {
            setTimeout(() => inputEl.focus(), 120);
        }
        if (!open) {
            skipToggle = false;
        }
    }

    function applySavedPosition() {
        if (isMobileView()) {
            chatRoot.style.left = '';
            chatRoot.style.top = '';
            chatRoot.style.right = '';
            chatRoot.style.bottom = '';
            chatRoot.dataset.positioned = '0';
            return;
        }
        const saved = readStorage(STORAGE_KEYS.position, null);
        if (!saved || typeof saved.left !== 'number' || typeof saved.top !== 'number') {
            return;
        }
        chatRoot.style.left = saved.left + 'px';
        chatRoot.style.top = saved.top + 'px';
        chatRoot.style.right = 'auto';
        chatRoot.style.bottom = 'auto';
        chatRoot.dataset.positioned = '1';
        clampToViewport();
    }

    function clampToViewport() {
        if (isMobileView()) {
            return;
        }
        if (chatRoot.dataset.positioned !== '1') {
            return;
        }
        const rect = chatRoot.getBoundingClientRect();
        const maxLeft = Math.max(0, window.innerWidth - rect.width);
        const maxTop = Math.max(0, window.innerHeight - rect.height);
        const left = Math.min(Math.max(0, rect.left), maxLeft);
        const top = Math.min(Math.max(0, rect.top), maxTop);
        chatRoot.style.left = left + 'px';
        chatRoot.style.top = top + 'px';
        chatRoot.style.right = 'auto';
        chatRoot.style.bottom = 'auto';
    }

    function savePosition(left, top) {
        writeStorage(STORAGE_KEYS.position, { left, top });
        chatRoot.dataset.positioned = '1';
    }

    function handleToggle() {
        if (skipToggle) {
            skipToggle = false;
            return;
        }
        const isOpen = chatRoot.classList.contains('open');
        setOpen(!isOpen);
    }

    function handleDocumentClick(event) {
        if (!chatRoot.classList.contains('open')) {
            return;
        }
        if (isMobileView()) {
            return;
        }
        if (chatRoot.contains(event.target)) {
            return;
        }
        setOpen(false);
    }

    function sendMessage() {
        const question = inputEl.value.trim();
        const model = modelSelect.value;
        if (!question) {
            return;
        }
        if (!isAuthenticated) {
            addMessage('system', '请先登录后使用AI问答。');
            return;
        }
        if (!model) {
            addMessage('system', '当前没有可用的模型，请联系管理员。');
            return;
        }

        inputEl.value = '';
        addMessage('user', question);
        const loadingNode = addMessage('assistant', '正在思考...', { skipStore: true, loading: true });

        fetch('/api/ai/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question: question, model: model })
        })
            .then(response => response.json())
            .then(data => {
                if (loadingNode && loadingNode.parentNode) {
                    loadingNode.parentNode.removeChild(loadingNode);
                }
                if (data.success) {
                    if (data.triage && data.triage.is_emergency) {
                        const actions = (data.triage.actions || []).join(' ');
                        const keywords = (data.triage.matched_keywords || []).join('、');
                        const warningText = '紧急提醒：' + (actions || '请优先就医或拨打120。') +
                            (keywords ? ' 识别到关键词：' + keywords + '。' : '') +
                            '（提示仅供参考）';
                        addMessage('system', warningText);
                    }
                    addMessage('assistant', data.answer || '');
                } else {
                    addMessage('system', data.error || '请求失败，请稍后再试。');
                }
            })
            .catch(error => {
                if (loadingNode && loadingNode.parentNode) {
                    loadingNode.parentNode.removeChild(loadingNode);
                }
                addMessage('system', '请求失败：' + error);
            });
    }

    function handleKeyDown(event) {
        if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            sendMessage();
        }
    }

    function startDrag(event) {
        if (isMobileView()) {
            return;
        }
        if (event.button !== 0) {
            return;
        }
        const rect = chatRoot.getBoundingClientRect();
        dragState = {
            offsetX: event.clientX - rect.left,
            offsetY: event.clientY - rect.top,
            moved: false
        };
        chatRoot.classList.add('dragging');
        event.preventDefault();
    }

    function onDrag(event) {
        if (!dragState) {
            return;
        }
        dragState.moved = true;
        const left = event.clientX - dragState.offsetX;
        const top = event.clientY - dragState.offsetY;
        chatRoot.style.left = left + 'px';
        chatRoot.style.top = top + 'px';
        chatRoot.style.right = 'auto';
        chatRoot.style.bottom = 'auto';
    }

    function stopDrag() {
        if (!dragState) {
            return;
        }
        chatRoot.classList.remove('dragging');
        clampToViewport();
        const rect = chatRoot.getBoundingClientRect();
        savePosition(Math.round(rect.left), Math.round(rect.top));
        if (dragState.moved) {
            skipToggle = true;
        }
        dragState = null;
    }

    function restoreModel() {
        const savedModel = readStorage(STORAGE_KEYS.model, null);
        if (!savedModel || !modelSelect) {
            return;
        }
        const option = Array.from(modelSelect.options).find(item => item.value === savedModel);
        if (option) {
            modelSelect.value = savedModel;
        }
    }

    function initState() {
        try {
            localStorage.removeItem(STORAGE_PREFIX + 'history');
        } catch (error) {
        }
        chatHistory = [];
        renderHistory();
        applySavedPosition();
        const open = readStorage(STORAGE_KEYS.open, 0) === 1;
        setOpen(open);
        restoreModel();
        if (!isAuthenticated) {
            inputEl.setAttribute('disabled', 'disabled');
            sendButton.setAttribute('disabled', 'disabled');
        }
    }

    toggleButton.addEventListener('click', handleToggle);
    closeButton.addEventListener('click', () => setOpen(false));
    sendButton.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', handleKeyDown);
    document.addEventListener('click', handleDocumentClick);

    modelSelect.addEventListener('change', function() {
        writeStorage(STORAGE_KEYS.model, modelSelect.value);
    });

    header.addEventListener('mousedown', startDrag);
    toggleButton.addEventListener('mousedown', startDrag);
    document.addEventListener('mousemove', onDrag);
    document.addEventListener('mouseup', stopDrag);

    window.addEventListener('resize', function() {
        applySavedPosition();
        clampToViewport();
    });

    window.AIChat = {
        open: function() {
            setOpen(true);
        },
        close: function() {
            setOpen(false);
        }
    };

    initState();
})();
