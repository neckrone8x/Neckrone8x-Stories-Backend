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
