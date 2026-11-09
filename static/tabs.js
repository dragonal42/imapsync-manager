document.querySelectorAll('[data-tabs]').forEach(group => {
    const list = group.querySelector('[data-tab-list]');
    const tabs = Array.from(list.querySelectorAll('[data-tab]'));
    const panels = tabs.map(tab => document.getElementById(tab.dataset.tab));
    list.setAttribute('role', 'tablist');
    tabs.forEach((tab, index) => {
        tab.id = panels[index].id + '-tab';
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-controls', panels[index].id);
        panels[index].setAttribute('role', 'tabpanel');
        panels[index].setAttribute('aria-labelledby', tab.id);
    });
    function select(index, focus = false) {
        tabs.forEach((tab, i) => {
            tab.setAttribute('aria-selected', String(i === index));
            tab.tabIndex = i === index ? 0 : -1;
            panels[i].hidden = i !== index;
        });
        if (focus) tabs[index].focus();
    }
    tabs.forEach((tab, index) => {
        tab.addEventListener('click', () => select(index));
        tab.addEventListener('keydown', event => {
            const next = {ArrowLeft: (index + tabs.length - 1) % tabs.length,
                ArrowRight: (index + 1) % tabs.length, Home: 0, End: tabs.length - 1}[event.key];
            if (next === undefined) return;
            event.preventDefault();
            select(next, true);
        });
    });
    // Reveal the first invalid field even when it belongs to an inactive tab.
    group.addEventListener('invalid', event => {
        const form = event.target.form;
        const first = form?.querySelector(':invalid');
        if (first !== event.target) return;
        const index = panels.findIndex(panel => panel.contains(first));
        if (index >= 0) select(index);
    }, true);
    select(0);
    list.hidden = false;
});
