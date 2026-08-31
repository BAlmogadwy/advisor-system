/* Shared adviser/admin graduation workspace controls. */
(function () {
  'use strict';

  const tablist = document.getElementById('agBaselineTabs');
  const tabs = Array.from(tablist?.querySelectorAll('[role="tab"][href]') || []);
  const invalidStudentId = document.querySelector(
    '#adminGraduationStudentId[aria-invalid="true"]'
  );
  const scenarioForm = document.getElementById('adminGradScenarioForm');
  const mustHaveInput = document.getElementById('adminGradMustHave');
  const planPicker = document.getElementById('adminGradPlanPicker');
  const addCourseButton = document.getElementById('adminGradAddCourse');
  const selectionStatus = document.getElementById('adminGradSelectionStatus');
  const scenarioError = document.getElementById('adminGradScenarioErrors');

  if (invalidStudentId) {
    requestAnimationFrame(function () {
      invalidStudentId.focus({preventScroll: true});
    });
  }

  function scenarioCodes() {
    const seen = new Set();
    return String(mustHaveInput?.value || '')
      .split(/[,;\r\n]+/)
      .map(function (value) { return value.trim().toUpperCase().replace(/\s+/g, ''); })
      .filter(function (code) {
        if (!code || seen.has(code)) return false;
        seen.add(code);
        return true;
      });
  }

  function writeScenarioCodes(codes) {
    if (!mustHaveInput) return;
    mustHaveInput.value = codes.join(', ');
    mustHaveInput.setCustomValidity('');
  }

  function renderScenarioChips() {
    if (!selectionStatus || !scenarioForm) return;
    const codes = scenarioCodes();
    selectionStatus.replaceChildren();
    if (!codes.length) {
      const empty = document.createElement('span');
      empty.className = 'admin-grad-selection-empty';
      empty.textContent = scenarioForm.dataset.emptyLabel || 'No must-have courses added yet.';
      selectionStatus.appendChild(empty);
      return;
    }
    codes.forEach(function (code) {
      const chip = document.createElement('span');
      chip.className = 'admin-grad-selected-chip';
      chip.dataset.courseCode = code;
      const label = document.createElement('bdi');
      label.dir = 'ltr';
      label.textContent = code;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.dataset.removeCourse = code;
      remove.setAttribute(
        'aria-label',
        `${scenarioForm.dataset.removeLabel || 'Remove course from scenario'}: ${code}`
      );
      remove.textContent = '×';
      chip.append(label, remove);
      selectionStatus.appendChild(chip);
    });
  }

  function addPickedCourse() {
    if (!planPicker || !mustHaveInput || !scenarioForm) return;
    const code = String(planPicker.value || '').trim().toUpperCase();
    if (!code) {
      planPicker.focus();
      return;
    }
    const codes = scenarioCodes();
    const maximum = Number.parseInt(scenarioForm.dataset.maxCourses || '10', 10);
    if (!codes.includes(code) && codes.length >= maximum) {
      mustHaveInput.setCustomValidity(`At most ${maximum} courses can be added.`);
      mustHaveInput.reportValidity();
      mustHaveInput.focus();
      return;
    }
    if (!codes.includes(code)) codes.push(code);
    writeScenarioCodes(codes);
    renderScenarioChips();
    planPicker.value = '';
    mustHaveInput.focus();
  }

  if (scenarioForm && mustHaveInput) {
    mustHaveInput.addEventListener('input', renderScenarioChips);
    selectionStatus?.addEventListener('click', function (event) {
      const button = event.target.closest('button[data-remove-course]');
      if (!button) return;
      const code = button.dataset.removeCourse;
      writeScenarioCodes(scenarioCodes().filter(function (value) { return value !== code; }));
      renderScenarioChips();
      mustHaveInput.focus();
    });
    addCourseButton?.addEventListener('click', addPickedCourse);
    planPicker?.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      addPickedCourse();
    });
    scenarioForm.addEventListener('submit', function (event) {
      const codes = scenarioCodes();
      const maximum = Number.parseInt(scenarioForm.dataset.maxCourses || '10', 10);
      if (codes.length > maximum) {
        event.preventDefault();
        mustHaveInput.setCustomValidity(`At most ${maximum} courses can be added.`);
        mustHaveInput.reportValidity();
        mustHaveInput.focus();
        return;
      }
      scenarioForm.querySelectorAll('input[data-generated-must-have]').forEach(function (node) {
        node.remove();
      });
      mustHaveInput.disabled = true;
      codes.forEach(function (code) {
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'must_have';
        hidden.value = code;
        hidden.dataset.generatedMustHave = 'true';
        scenarioForm.appendChild(hidden);
      });
    });
    renderScenarioChips();
  }

  if (scenarioError) {
    requestAnimationFrame(function () {
      scenarioError.focus({preventScroll: true});
    });
  }

  if (tablist && tabs.length) {
    tablist.addEventListener('keydown', function (event) {
      if (!tabs.includes(event.target)) return;
      const current = tabs.indexOf(event.target);
      let next = null;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        next = (current + 1) % tabs.length;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        next = (current - 1 + tabs.length) % tabs.length;
      } else if (event.key === 'Home') {
        next = 0;
      } else if (event.key === 'End') {
        next = tabs.length - 1;
      }
      if (next === null) return;
      event.preventDefault();
      tabs[next].focus();
      tabs[next].click();
    });
  }

  /* Keep the owning sidebar entry highlighted on nested result routes. */
  const workspace = document.querySelector('main[data-graduation-workspace]')
    ?.getAttribute('data-graduation-workspace');
  const activeHref = workspace === 'admin' ? '/graduation-planning/' : '/advisor-portfolio/';
  document.querySelectorAll('.sidebar .nav-link').forEach(function (link) {
    if (link.getAttribute('href') === activeHref) link.classList.add('active');
  });
})();
