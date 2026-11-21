/* Neckrone8x Stories: theme toggle, copy link, reading progress. No libraries.
   Every block only runs if its elements are on the page. */
(function () {
    'use strict';

    var root = document.documentElement;

    /* ---------- THEME ---------- */
    var toggle = document.getElementById('themeToggle');
    var themeColor = document.querySelector('meta[name="theme-color"]');

    function applyTheme(theme) {
        root.setAttribute('data-theme', theme);
        if (toggle) {
            toggle.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
        }
        if (themeColor) themeColor.setAttribute('content', theme === 'dark' ? '#0f172a' : '#f8fafc');
    }

    applyTheme(root.getAttribute('data-theme') === 'light' ? 'light' : 'dark');

    if (toggle) {
        toggle.addEventListener('click', function () {
            var next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
            applyTheme(next);
            try { localStorage.setItem('theme', next); } catch (e) { /* private mode */ }
        });
    }

    /* ---------- COPY LINK ---------- */
    var copyButton = document.getElementById('copyLink');
    var copyMessage = document.getElementById('copyMessage');

    function copied() {
        if (!copyMessage) return;
        copyMessage.textContent = 'Link copied';
        setTimeout(function () { copyMessage.textContent = ''; }, 2500);
    }

    function fallbackCopy(text) {
        var area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.left = '-9999px';
        document.body.appendChild(area);
        area.select();
        try { document.execCommand('copy'); copied(); }
        catch (e) { if (copyMessage) copyMessage.textContent = 'Could not copy the link'; }
        document.body.removeChild(area);
    }

    if (copyButton) {
        copyButton.addEventListener('click', function () {
            var url = copyButton.getAttribute('data-url') || window.location.href;
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(url).then(copied, function () { fallbackCopy(url); });
            } else {
                fallbackCopy(url);
            }
        });
    }

    /* ---------- REACTIONS (two sets on the story page, kept in step) ----------
       Without JavaScript each button is a normal form post. With it, the post
       is sent in the background and every button is updated from the reply. */
    var reactionForms = document.querySelectorAll('form.reaction-form');
    var reactionStatus = document.getElementById('reactionStatus');
    var reactionError = document.getElementById('reactionError');
    var reacting = false;

    function showReactions(state) {
        var buttons = document.querySelectorAll('.reaction[data-reaction]');
        for (var i = 0; i < buttons.length; i++) {
            var button = buttons[i];
            var type = button.getAttribute('data-reaction');
            var count = (state.counts && state.counts[type]) || 0;
            var mine = state.mine === type;
            button.classList.toggle('is-active', mine);
            button.setAttribute('aria-pressed', mine ? 'true' : 'false');
            button.setAttribute('aria-label', button.getAttribute('data-label') + ', ' + count +
                                (count === 1 ? ' reaction' : ' reactions'));
            var number = button.querySelector('.reaction-count');
            if (number) {
                number.textContent = String(count);
                number.classList.toggle('is-zero', count === 0);
            }
        }
    }

    function reactionMessage(text, isError) {
        if (reactionError) {
            reactionError.textContent = isError ? text : '';
            reactionError.hidden = !isError;
        }
        if (reactionStatus && !isError) reactionStatus.textContent = text;
    }

    if (reactionForms.length && window.fetch) {
        Array.prototype.forEach.call(reactionForms, function (form) {
            form.addEventListener('submit', function (event) {
                event.preventDefault();
                if (reacting) return;
                reacting = true;
                var button = form.querySelector('.reaction');
                var label = button ? button.getAttribute('data-label') : '';
                fetch(form.action, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
                }).then(function (response) {
                    return response.json().catch(function () { return null; }).then(function (data) {
                        return { ok: response.ok && !!data, status: response.status, data: data };
                    });
                }, function () {
                    form.submit();  // network failure before any reply: fall back to a normal post
                    return null;
                }).then(function (result) {
                    if (!result) return;
                    if (result.ok) {
                        showReactions(result.data);
                        reactionMessage(result.data.mine ? 'You reacted: ' + label : 'Your reaction was removed', false);
                    } else if (result.status === 429) {
                        reactionMessage('You have reacted a lot. Please wait a little and try again.', true);
                    } else {
                        reactionMessage('Your reaction could not be saved. Please try again.', true);
                    }
                }).catch(function () {
                    reactionMessage('Your reaction could not be saved. Please try again.', true);
                }).then(function () {
                    reacting = false;
                });
            });
        });
    }

    /* ---------- READING PROGRESS ---------- */
    var story = document.getElementById('story');
    var bar = document.getElementById('progressBar');

    if (story && bar) {
        var ticking = false;
        var update = function () {
            ticking = false;
            var top = story.getBoundingClientRect().top + window.scrollY;
            var total = story.offsetHeight - window.innerHeight;
            var progress = total <= 0 ? 100 : ((window.scrollY - top) / total) * 100;
            bar.style.width = Math.max(0, Math.min(100, progress)) + '%';
        };
        var onScroll = function () {
            if (!ticking) { ticking = true; window.requestAnimationFrame(update); }
        };
        window.addEventListener('scroll', onScroll, { passive: true });
        window.addEventListener('resize', onScroll);
        update();
    }
})();
