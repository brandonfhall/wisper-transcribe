/* wisper-transcribe web UI — minimal vanilla JS
   Handles: SSE log display, file upload feedback.
   HTMX handles all partial page updates and polling.
*/

// Log terminal auto-scroll
function autoScroll(el) {
  if (el) el.scrollTop = el.scrollHeight;
}

// File upload: show file size hint
document.addEventListener('DOMContentLoaded', function () {
  var fileInput = document.querySelector('input[type="file"][name="file"]');
  var hint = document.getElementById('file-size-hint');
  if (fileInput && hint) {
    fileInput.addEventListener('change', function (e) {
      var f = e.target.files[0];
      if (f) {
        var mb = (f.size / 1048576).toFixed(1);
        hint.textContent = 'Selected: ' + f.name + ' (' + mb + ' MB)';
      }
    });
  }

  // Auto-scroll any existing log terminal
  var terminal = document.getElementById('log-terminal');
  if (terminal) autoScroll(terminal);
});
