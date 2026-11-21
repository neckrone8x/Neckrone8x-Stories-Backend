/* Neckrone8x Stories: theme toggle, copy link and share, reactions, reading
   progress, bookmarks, continue reading, reading settings. No libraries.
   Every block only runs if its elements are on the page. */
(function () {
    'use strict';

    var root = document.documentElement;

    /* ---------- BROWSER STORAGE ----------
       Everything the reader keeps (saved stories, progress, settings) is in
       localStorage. It can be blocked (private windows, strict settings), so
       every access is wrapped; when it is blocked, store.ok is false and the
       features that need it stay hidden. */
    var store = (function () {
        var area = null;
        try {
            area = window.localStorage;
            area.setItem('n8x.test', '1');
            area.removeItem('n8x.test');
        } catch (e) {
            area = null;
        }
        return {
            ok: !!area,
            get: function (key, fallback) {
                if (!area) return fallback;
                try {
                    var value = JSON.parse(area.getItem(key));
                    return value === null || value === undefined ? fallback : value;
                } catch (e) {
                    return fallback;
                }
            },
            set: function (key, value) {
                if (!area) return false;
                try { area.setItem(key, JSON.stringify(value)); return true; } catch (e) { return false; }
            },
            remove: function (key) {
                if (!area) return;
                try { area.removeItem(key); } catch (e) { /* ignore */ }
            }
        };
    })();

    var liveStatus = document.getElementById('liveStatus');
    function announce(text) {
        if (!liveStatus) return;
        liveStatus.textContent = '';
        setTimeout(function () { liveStatus.textContent = text; }, 50);
    }

    var SLUG = /^[a-z0-9][a-z0-9-]{0,219}$/;
    function isSlug(value) { return typeof value === 'string' && SLUG.test(value); }

    function each(list, fn) { Array.prototype.forEach.call(list, fn); }

    /* Ask the server which of these slugs are published stories (same order). */
    function loadStories(endpoint, slugs) {
        var url = endpoint + '?slugs=' + encodeURIComponent(slugs.join(','));
        return fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
            .then(function (response) {
                if (!response.ok) throw new Error('status ' + response.status);
                return response.json();
            })
            .then(function (data) { return (data && data.stories) || []; });
    }

    /* Fill a <template> copy: elements marked data-field="name". */
    function field(node, name) { return node.querySelector('[data-field="' + name + '"]'); }

    function fillTag(tag, story) {
        tag.textContent = story.adult ? '18+' : story.category;
        if (story.adult) tag.className = 'tag tag-adult';
    }

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

    /* ---------- MORE CATEGORIES ----------
       A native <details> (works without the script); this only adds closing
       with Escape (focus back on "More") and with a click elsewhere. */
    var chipsMore = document.getElementById('chipsMore');
    if (chipsMore) {
        var moreSummary = chipsMore.querySelector('summary');
        chipsMore.addEventListener('keydown', function (event) {
            if ((event.key === 'Escape' || event.key === 'Esc') && chipsMore.open) {
                event.preventDefault();
                chipsMore.open = false;
                if (moreSummary) moreSummary.focus();
            }
        });
        document.addEventListener('click', function (event) {
            if (chipsMore.open && !chipsMore.contains(event.target)) chipsMore.open = false;
        });
    }

    /* ---------- SHARE SHEET ----------
       On phones and tablets with a share sheet, one Share button replaces the
       WhatsApp / Facebook / X / Copy link buttons. Desktop browsers keep them.
       The shared link is the story's clean address (no tracking added). */
    var share = document.querySelector('.share[data-share-url]');
    var shareNative = document.getElementById('shareNative');
    var touch = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;

    function showShareAlternatives(show) {
        if (!share) return;
        each(share.querySelectorAll('.share-alt'), function (item) { item.hidden = !show; });
    }

    if (share && shareNative && navigator.share && touch) {
        shareNative.hidden = false;
        showShareAlternatives(false);
        shareNative.addEventListener('click', function () {
            var title = share.getAttribute('data-share-title') || document.title;
            navigator.share({
                title: title,
                text: 'Read this story on Neckrone8x: ' + title,
                url: share.getAttribute('data-share-url')
            }).catch(function (error) {
                // closing the sheet is not an error; anything else: offer the other buttons
                if (!error || error.name !== 'AbortError') showShareAlternatives(true);
            });
        });
    }

    /* ---------- BOOKMARKS ----------
       A list of slugs, newest first, at most 50. Every button for the same
       story (story page, cards) shows the same state. */
    var SAVED_KEY = 'n8x.saved';
    var SAVED_MAX = 50;

    function savedSlugs() {
        var list = store.get(SAVED_KEY, []);
        return Array.isArray(list) ? list.filter(isSlug).slice(0, SAVED_MAX) : [];
    }

    function setSaved(slug, on) {
        var list = savedSlugs().filter(function (s) { return s !== slug; });
        if (on) list.unshift(slug);
        return store.set(SAVED_KEY, list.slice(0, SAVED_MAX));
    }

    function showBookmarks() {
        var saved = savedSlugs();
        each(document.querySelectorAll('[data-bookmark]'), function (button) {
            var on = saved.indexOf(button.getAttribute('data-bookmark')) !== -1;
            button.setAttribute('aria-pressed', on ? 'true' : 'false');
            button.classList.toggle('is-active', on);
        });
    }

    var bookmarkButtons = document.querySelectorAll('[data-bookmark]');
    if (store.ok && bookmarkButtons.length) {
        showBookmarks();
        each(bookmarkButtons, function (button) {
            button.hidden = false;
            button.addEventListener('click', function (event) {
                event.preventDefault();
                event.stopPropagation();   // a card's link covers the card; this button sits above it
                var slug = button.getAttribute('data-bookmark');
                var on = button.getAttribute('aria-pressed') !== 'true';
                if (!isSlug(slug) || !setSaved(slug, on)) {
                    announce('This browser could not save the story.');
                    return;
                }
                showBookmarks();
                announce(on ? 'Saved for later. Find it under Saved.' : 'Removed from saved stories.');
            });
        });
    }

    /* ---------- SAVED STORIES PAGE ---------- */
    var savedList = document.getElementById('savedList');

    function showOnly(id) {
        each(['savedLoading', 'savedEmpty', 'savedBlocked', 'savedError', 'savedList'], function (name) {
            var node = document.getElementById(name);
            if (node) node.hidden = name !== id;
        });
    }

    function savedCount(n) {
        var count = document.getElementById('savedCount');
        if (count) count.textContent = n ? n + (n === 1 ? ' story' : ' stories') : '';
    }

    function savedCard(template, story) {
        var card = template.content.firstElementChild.cloneNode(true);
        fillTag(field(card, 'tag'), story);
        var link = field(card, 'link');
        link.textContent = story.title;
        link.href = story.url;
        field(card, 'excerpt').textContent = story.adult
            ? 'Adult story. You will be asked to confirm your age before reading.'
            : (story.excerpt || '');
        field(card, 'author').textContent = 'By ' + story.author;
        field(card, 'minutes').textContent = story.reading_minutes + ' min read';
        var date = field(card, 'date');
        date.textContent = story.published_label;
        date.setAttribute('datetime', story.published_at);
        var remove = field(card, 'remove');
        remove.setAttribute('aria-label', 'Remove “' + story.title + '” from saved');
        remove.addEventListener('click', function () {
            setSaved(story.slug, false);
            var next = card.nextElementSibling || card.previousElementSibling;
            card.parentNode.removeChild(card);
            var left = savedList.children.length;
            savedCount(left);
            announce('Removed “' + story.title + '” from saved stories.');
            if (!left) {
                showOnly('savedEmpty');
                var title = document.getElementById('saved-title');
                if (title) title.focus();
            } else if (next) {
                next.querySelector('.saved-remove').focus();
            }
        });
        return card;
    }

    function loadSavedPage() {
        var template = document.getElementById('savedTemplate');
        if (!store.ok) { showOnly('savedBlocked'); return; }
        var slugs = savedSlugs();
        if (!slugs.length) { showOnly('savedEmpty'); savedCount(0); return; }
        showOnly('savedLoading');
        loadStories(savedList.getAttribute('data-endpoint'), slugs).then(function (stories) {
            // stories that were deleted or taken down are dropped from the list
            store.set(SAVED_KEY, stories.map(function (s) { return s.slug; }));
            savedList.innerHTML = '';
            stories.forEach(function (story) { savedList.appendChild(savedCard(template, story)); });
            savedCount(stories.length);
            showOnly(stories.length ? 'savedList' : 'savedEmpty');
        }).catch(function () {
            showOnly('savedError');
        });
    }

    if (savedList && window.fetch) {
        loadSavedPage();
        var retry = document.getElementById('savedRetry');
        if (retry) retry.addEventListener('click', loadSavedPage);
    } else if (savedList) {
        showOnly('savedError');
    }

    /* ---------- CONTINUE READING ----------
       Progress per story: { slug: { p: percent, t: time } }, newest 30 kept.
       A story leaves the list once it is read to the end (95%). */
    var PROGRESS_KEY = 'n8x.progress';
    var PROGRESS_KEEP = 30;

    function progressMap() {
        var map = store.get(PROGRESS_KEY, {});
        return map && typeof map === 'object' && !Array.isArray(map) ? map : {};
    }

    function saveProgressMap(map) {
        var slugs = Object.keys(map).filter(function (slug) {
            var item = map[slug];
            return isSlug(slug) && item && typeof item.p === 'number' && typeof item.t === 'number';
        }).sort(function (a, b) { return map[b].t - map[a].t; });
        var kept = {};
        slugs.slice(0, PROGRESS_KEEP).forEach(function (slug) { kept[slug] = map[slug]; });
        store.set(PROGRESS_KEY, kept);
    }

    var continueBox = document.getElementById('continueReading');
    if (continueBox && store.ok && window.fetch) {
        var map = progressMap();
        var open = Object.keys(map).filter(function (slug) {
            var item = map[slug];
            return isSlug(slug) && item && item.p >= 5 && item.p < 95;
        }).sort(function (a, b) { return map[b].t - map[a].t; }).slice(0, 3);

        if (open.length) {
            loadStories(continueBox.getAttribute('data-endpoint'), open).then(function (stories) {
                var list = document.getElementById('continueList');
                var template = document.getElementById('continueTemplate');
                if (!stories.length || !list || !template) return;
                stories.forEach(function (story) {
                    var percent = Math.round(map[story.slug].p);
                    var card = template.content.firstElementChild.cloneNode(true);
                    fillTag(field(card, 'tag'), story);
                    var link = field(card, 'link');
                    link.textContent = story.title;
                    link.href = story.url;
                    link.addEventListener('click', function () {
                        try { window.sessionStorage.setItem('n8x.resume', story.slug); } catch (e) { /* ignore */ }
                    });
                    field(card, 'stats').textContent = percent + '% read · ' + story.reading_minutes + ' min story';
                    field(card, 'bar').style.width = percent + '%';
                    list.appendChild(card);
                });
                continueBox.hidden = false;
            }).catch(function () { /* the row simply stays hidden */ });
        }

        var clear = document.getElementById('continueClear');
        if (clear) {
            clear.addEventListener('click', function () {
                store.remove(PROGRESS_KEY);
                continueBox.hidden = true;
                announce('Continue reading cleared.');
                var heading = document.getElementById('listing-title');
                if (heading) heading.focus();
            });
        }
    }

    /* On a story page: remember how far the reader got (at most once a
       second while scrolling, and when they leave). */
    var storyArticle = document.getElementById('story');
    var storyText = document.querySelector('.story-body');
    var storySlug = storyArticle ? storyArticle.getAttribute('data-slug') : null;

    function readPercent() {
        var box = storyText.getBoundingClientRect();
        var view = window.innerHeight || document.documentElement.clientHeight;
        if (box.bottom <= view) return 100;               // the end of the text is on screen
        var range = box.height - view;
        return range <= 0 ? 100 : Math.max(0, Math.min(100, (-box.top / range) * 100));
    }

    if (store.ok && storyText && isSlug(storySlug)) {
        var finished = false;
        var scrolled = false;
        var saveTimer = null;

        var saveProgress = function () {
            saveTimer = null;
            if (finished || !scrolled) return;
            var percent = Math.round(readPercent());
            var all = progressMap();
            if (percent >= 95) {
                finished = true;                           // read to the end: off the list
                delete all[storySlug];
            } else if (percent >= 1) {
                all[storySlug] = { p: percent, t: Date.now() };
            } else {
                return;
            }
            saveProgressMap(all);
        };

        window.addEventListener('scroll', function () {
            scrolled = true;
            if (!saveTimer) saveTimer = setTimeout(saveProgress, 1000);
        }, { passive: true });
        window.addEventListener('pagehide', saveProgress);
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') saveProgress();
        });

        // came here from "Continue reading": go back to where they stopped
        var resume = null;
        try {
            resume = window.sessionStorage.getItem('n8x.resume');
            window.sessionStorage.removeItem('n8x.resume');
        } catch (e) { /* ignore */ }
        var saved = progressMap()[storySlug];
        if (resume === storySlug && saved && saved.p >= 5 && saved.p < 95) {
            var box = storyText.getBoundingClientRect();
            var view = window.innerHeight || document.documentElement.clientHeight;
            var top = window.pageYOffset + box.top + (box.height - view) * saved.p / 100;
            root.style.scrollBehavior = 'auto';
            window.scrollTo(0, Math.max(0, top));
            root.style.scrollBehavior = '';
            announce('Picked up where you left off.');
        }
    }

    /* ---------- READING SETTINGS (story page) ----------
       Text size and sepia. The head script applies the saved choice before
       the page is drawn; this keeps the panel and the page in step. */
    var READING_KEY = 'n8x.reading';
    var readingToggle = document.getElementById('readingToggle');
    var readingPanel = document.getElementById('readingPanel');

    function readingPrefs() {
        var prefs = store.get(READING_KEY, {});
        prefs = prefs && typeof prefs === 'object' ? prefs : {};
        return {
            size: /^(s|m|l|xl)$/.test(prefs.size) ? prefs.size : 'm',
            sepia: prefs.sepia === true
        };
    }

    // without storage the settings still work for this page, just not remembered
    var currentPrefs = readingPrefs();
    if (!store.ok) {
        currentPrefs.size = root.getAttribute('data-text-size') || 'm';
        currentPrefs.sepia = root.getAttribute('data-sepia') === 'on';
    }

    function applyReading(prefs) {
        if (prefs.size === 'm') root.removeAttribute('data-text-size');
        else root.setAttribute('data-text-size', prefs.size);
        if (prefs.sepia) root.setAttribute('data-sepia', 'on');
        else root.removeAttribute('data-sepia');
        store.set(READING_KEY, prefs);
    }

    function setReadingOpen(open, returnFocus) {
        readingPanel.hidden = !open;
        readingToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) {
            var checked = readingPanel.querySelector('input[name="text-size"]:checked');
            if (checked) checked.focus();
        } else if (returnFocus) {
            readingToggle.focus();
        }
    }

    if (readingToggle && readingPanel) {
        readingToggle.hidden = false;
        each(readingPanel.querySelectorAll('input[name="text-size"]'), function (input) {
            input.checked = input.value === currentPrefs.size;
            input.addEventListener('change', function () {
                if (!input.checked) return;
                currentPrefs.size = input.value;
                applyReading(currentPrefs);
            });
        });
        var sepia = document.getElementById('sepiaToggle');
        if (sepia) {
            sepia.checked = currentPrefs.sepia;
            sepia.addEventListener('change', function () {
                currentPrefs.sepia = sepia.checked;
                applyReading(currentPrefs);
            });
        }
        readingToggle.addEventListener('click', function () {
            setReadingOpen(readingPanel.hidden, false);
        });
        var done = document.getElementById('readingDone');
        if (done) done.addEventListener('click', function () { setReadingOpen(false, true); });
        readingPanel.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' || event.key === 'Esc') {
                event.preventDefault();
                setReadingOpen(false, true);
            }
        });
        document.addEventListener('click', function (event) {
            if (readingPanel.hidden) return;
            if (!readingPanel.contains(event.target) && !readingToggle.contains(event.target)) {
                setReadingOpen(false, false);
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
