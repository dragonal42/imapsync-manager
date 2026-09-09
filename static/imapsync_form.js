$(document).ready(function () {
    "use strict";
    $("[data-toggle='tooltip']").tooltip();

    var readyStateStr = {
        "0": "Request not initialized", "1": "Server connection established",
        "2": "Response headers received", "3": "Processing request", "4": "Finished and response is ready"
    };

    var refresh_interval_ms = 2000; // adjusted for smoother polling
    var refresh_interval_s = refresh_interval_ms / 1000;

    var decompose_eta_line = function decompose_eta_line(eta_str) {
        var regex_eta = /^ETA:\s+(.*?)\s+([0-9]+)\s+s\s+([0-9]+)\/([0-9]+)\s+msgs\s+left\n?$/;
        var eta_array = regex_eta.exec(eta_str);
        if (eta_array !== null) {
            return {
                str: eta_str, date: eta_array[1], seconds_left: eta_array[2], msgs_left: eta_array[3], msgs_total: eta_array[4],
                msgs_done: function() { return (this.msgs_total - this.msgs_left).toString(); },
                percent_done: function() { return this.msgs_total == 0 ? "0" : ((this.msgs_total - this.msgs_left) / this.msgs_total * 100).toFixed(2); },
                percent_left: function() { return this.msgs_total == 0 ? "0" : (this.msgs_left / this.msgs_total * 100).toFixed(2); }
            };
        }
        return { str: "", msgs_left: "?", msgs_total: "?", percent_done: function() { return "0"; }, percent_left: function() { return "0"; } };
    };

    var last_eta = function last_eta(string) {
        if (!string) return "";
        var eta = string.match(/ETA:.*\n/g);
        return eta ? eta[eta.length - 1] : "ETA: unknown";
    };

    var extract_eta = function extract_eta(xhr) {
        var slice = xhr.responseText.slice(-24000);
        return decompose_eta_line(last_eta(slice));
    };

    var progress_bar_update = function progress_bar_update(eta_obj) {
        if (eta_obj.str.length) {
            $("#progress-bar-done").css("width", eta_obj.percent_done() + "%").text(eta_obj.percent_done() + "% done");
            $("#progress-bar-left").css("width", eta_obj.percent_left() + "%").text(eta_obj.percent_left() + "% left");
        }
    };

    var refreshLog = function refreshLog(xhr) {
        var eta_obj = extract_eta(xhr);
        progress_bar_update(eta_obj);
        if (xhr.readyState === 4) {
            $("#progress-txt").text("Ended. It remains " + eta_obj.msgs_left + " messages to be synced");
            $("#output").text(xhr.responseText);
        } else {
            $("#progress-txt").text(eta_obj.str + " (refreshing...)");
            $("#output").text(xhr.responseText.split(/\r?\n/).slice(-15).join("\n"));
        }
    };

    var handleRun = function handleRun(xhr, timerRefreshLog) {
        $("#console").text("Status: " + xhr.status + " " + xhr.statusText + "\nState: " + readyStateStr[xhr.readyState]);
        if (xhr.readyState === 4) {
            clearInterval(timerRefreshLog);
            refreshLog(xhr);
            $("#bt-sync").prop("disabled", false);
        }
    };

    var imapsync = function imapsync() {
        var querystring = $("#form").serialize();
        $("#abort").text("\n\n\n");
        $("#output").text("Here comes the log!\n\n");

        var xhr = new XMLHttpRequest();
        var timerRefreshLog = setInterval(function () { refreshLog(xhr); }, refresh_interval_ms);
        xhr.onreadystatechange = function () { handleRun(xhr, timerRefreshLog); };
        xhr.open("POST", "/cgi-bin/imapsync", true);
        xhr.setRequestHeader("Content-type", "application/x-www-form-urlencoded");
        xhr.send(querystring);
    };

    var handleAbort = function handleAbort(xhr) {
        $("#abort").text("Status: " + xhr.status + "\nState: " + readyStateStr[xhr.readyState]);
        if (xhr.readyState === 4) {
            $("#abort").append(xhr.responseText);
            $("#bt-sync").prop("disabled", false);
            $("#bt-abort").prop("disabled", false);
        }
    };

    var abort = function abort() {
        var querystring = $("#form").serialize() + "&abort=on";
        var xhr = new XMLHttpRequest();
        xhr.onreadystatechange = function () { handleAbort(xhr); };
        xhr.open("POST", "/cgi-bin/imapsync", true);
        xhr.setRequestHeader("Content-type", "application/x-www-form-urlencoded");
        xhr.send(querystring);
    };

    var showpassword = function showpassword(id, button) {
        var x = document.getElementById(id);
        x.type = button.checked ? "text" : "password";
    };

    var init = function init() {
        $("#bt-sync").prop("disabled", false);
        $("#bt-abort").prop("disabled", false);
        
        $("#showpassword1").click(function (event) { showpassword("password1", event.target); });
        $("#showpassword2").click(function (event) { showpassword("password2", event.target); });

        $("#bt-sync").click(function () {
            $("#bt-sync").prop("disabled", true);
            $("#bt-abort").prop("disabled", false);
            $("#progress-txt").text("ETA: coming soon");
            imapsync();
        });

        $("#bt-abort").click(function () {
            $("#bt-sync").prop("disabled", true);
            $("#bt-abort").prop("disabled", true);
            abort();
        });

        $("#swap").click(function() {
            var swap = function(p1, p2) { var temp = $(p2).val(); $(p2).val($(p1).val()); $(p1).val(temp); };
            swap($("#user1"), $("#user2"));
            swap($("#password1"), $("#password2"));
            swap($("#host1"), $("#host2"));
            swap($("#subfolder1"), $("#subfolder2"));
        });
    };
    init();
});
