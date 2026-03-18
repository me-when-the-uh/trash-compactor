/* jshint strict: true, esversion: 5, browser: true */

var Util = (function()
{
	"use strict";

	var powers = '_KMGTPEZY';
	var monotime = function() { return Date.now(); };

	if (window.performance && window.performance.now)
		monotime = function() { return window.performance.now(); };

	return {
		debounce: function(callback, delay) {
			var timeout;
			var fn = function() {
				var context = this;
				var args = arguments;

				clearTimeout(timeout);
				timeout = setTimeout(function() {
					timeout = null;
					callback.apply(context, args);
				}, delay);
			};
			fn.clear = function() {
				clearTimeout(timeout);
				timeout = null;
			};

			return fn;
		},

		throttle: function(callback, delay) {
			var timeout;
			var last;
			var fn = function() {
				var context = this;
				var args = arguments;
				var now = monotime();

				if (last && now < last + delay) {
					clearTimeout(timeout);
					timeout = setTimeout(function() {
						timeout = null;
						last = now;
						callback.apply(context, args);
					}, delay);
				} else {
					last = now;
					callback.apply(context, args);
				}
			};
			fn.clear = function() {
				clearTimeout(timeout);
				timeout = null;
			};

			return fn;
		},

		format_number: function(number, digits) {
			if (digits === undefined) digits = 2;
			return number.toLocaleString("en", {minimumFractionDigits: digits, maximumFractionDigits: digits});
		},

		bytes_to_human_dec: function(bytes) {
			for (var i = powers.length - 1; i > 0; i--) {
				var div = Math.pow(10, 3 * i);
				if (bytes >= div) {
					return Util.format_number(bytes / div, 2) + " " + powers[i] + 'B';
				}
			}
			return Util.format_number(bytes, 0) + ' B';
		},

		bytes_to_human_bin: function(bytes) {
			for (var i = powers.length - 1; i > 0; i--) {
				var div = Math.pow(2, 10*i);
				if (bytes >= div) {
					return Util.format_number(bytes / div, 2) + " " + powers[i] + 'iB';
				}
			}
			return Util.format_number(bytes, 0) + ' B';
		},

		human_to_bytes: function(human) {
			if (!human) return null;
			var num = parseFloat(human);

			var match = (/\s*([KMGTPEZY])(i)?([Bb])?\s*$/i).exec(human);
			if (match) {
				var pow = (match[2] == 'i') ? 1024 : 1000;
				num *= Math.pow(pow, powers.indexOf(match[1].toUpperCase()));
			}

			return num;
		},

		number_to_human: function(num) {
			for (var i = powers.length - 1; i > 0; i--) {
				var div = Math.pow(10, 3*i);
				if (num >= div) {
					return Util.format_number(num / div, 2) + powers[i];
				}
			}
			return num;
		},

		human_to_number: function(human) {
			if (!human) return null;
			var num = parseFloat(human);

			var match = (/\s*([KMGTPEZY])\s*$/i).exec(human);

			if (match) {
				num *= Math.pow(1000, powers.indexOf(match[1].toUpperCase()));
			}

			return num;
		},

		sformat: function() {
			var args = arguments;
			return args[0].replace(/\{(\d+)\}/g, function (m, n) { return args[parseInt(n, 10) + 1]; });
		},

		range: function(a, b, step) {
			if (!step) step = 1;
			var arr = [];
			for (var i = a; i < b; i += step) {
				arr.push(i);
			}
			return arr;
		}
	};
})();

Util.bytes_to_human = Util.bytes_to_human_bin;

// Actions call back into Python
var Action = (function() {
	"use strict";

	function _dispatch_if_message(res) {
		if (res && res.type) {
			Response.dispatch(res);
		}
	}

	return {
		open_url: function(url) {
			pywebview.api.open_url(url).then(_dispatch_if_message);
		},

		reset_config: function() {
			pywebview.api.reset_config().then(_dispatch_if_message);
		},

		save_config: function(config) {
			config.type = 'SaveConfig';
			pywebview.api.save_config(config).then(_dispatch_if_message);
		},

		choose_folder: function() {
			pywebview.api.choose_folder().then(function(res) {
				if (res && res.type === 'Folder') {
					Response.dispatch(res);
					// Auto start analysis
					Action.analyse();
				}
			});
		},

		start_compression: function() {
			pywebview.api.start_compression().then(_dispatch_if_message);
		},

		pause: function() {
			pywebview.api.pause_compression().then(_dispatch_if_message);
		},

		resume: function() {
			pywebview.api.resume_compression().then(_dispatch_if_message);
		},

		analyse: function() {
			pywebview.api.analyse_folder().then(_dispatch_if_message);
		},

		stop: function() {
			pywebview.api.stop_compression().then(_dispatch_if_message);
		},

		get_progress: function() {
			pywebview.api.get_progress_update().then(_dispatch_if_message);
		}
	};
})();

// Responses come from Python
var Response = (function() {
	"use strict";

	return {
		dispatch: function(msg) {
			switch(msg.type) {
				case "Config":
                                        Gui.set_decimal(msg.decimal);
                                        Gui.set_min_savings(msg.min_savings);
                                        Gui.set_checkbox("No_LZX", msg.no_lzx);
                                        Gui.set_checkbox("Force_LZX", msg.force_lzx);
                                        Gui.set_checkbox("Single_Worker", msg.single_worker);
                                        break;

				case "Folder":
					Gui.set_folder(msg.path);
					break;

				case "Status":
					Gui.queue_status(msg.status, msg.pct);
					break;

				case "Paused":
				case "Resumed":
				case "Stopped":
				case "Scanning":
				case "Compacting":
					Gui[msg.type.toLowerCase()]();
					break;

				case "FolderSummary":
					Gui.queue_folder_summary(msg.info);
					break;

				case "ProgressUpdate":
					Gui.queue_status(msg.status, msg.pct);
					break;

				case "Warning":
					Gui.show_warning(msg.title, msg.message);
					break;
			}
		}
	};
})();

// Anything poking the GUI lives here
var Gui = (function() {
	"use strict";

	var status_queue = null;
	var folder_summary_queue = null;

	function _flush_queued_updates() {
		if (status_queue) {
			Gui.set_status(status_queue.status, status_queue.pct);
			status_queue = null;
		}
		if (folder_summary_queue) {
			Gui.set_folder_summary(folder_summary_queue);
			folder_summary_queue = null;
		}
	}

	return {
		boot: function() {
			$("a[href]").on("click", function(e) {
				e.preventDefault();
				Action.open_url($(this).attr("href"));
				return false;
			});

			$("#Button_Save").on("click", function() {
				Action.save_config({
					decimal: $("#SI_Units").val() == "D",
					min_savings: parseFloat($("#Min_Savings").val() || 18),
                                        no_lzx: $("#No_LZX").is(":checked"),
                                        force_lzx: $("#Force_LZX").is(":checked"),
                                        single_worker: $("#Single_Worker").is(":checked")
                                });
			});

			$("#Button_Reset").on("click", function() {
				Action.reset_config();
			});

			setInterval(_flush_queued_updates, 100);
			Action.reset_config();
		},

		queue_status: function(status, pct) {
			status_queue = {
				status: status,
				pct: pct
			};
		},

		queue_folder_summary: function(data) {
			folder_summary_queue = data;
		},

		page: function(page) {
			$("nav button").removeClass("active");
			$("#Button_Page_" + page).addClass("active");
			$("section.page").hide();
			$("#" + page).show();
		},

		version: function(date, version) {
			$(".compile-date").text(date);
			$(".version").text(version);
		},

		set_decimal: function(dec) {
			var field = $("#SI_Units");
			if (dec) {
				field.val("D");
				Util.bytes_to_human = Util.bytes_to_human_dec;
			} else {
				field.val("I");
				Util.bytes_to_human = Util.bytes_to_human_bin;
			}
		},

		set_checkbox: function(id, val) {
                        $("#" + id).prop("checked", !!val);
                },

                set_min_savings: function(min_savings) {
			$("#Min_Savings").val(min_savings || 18);
		},

		set_folder: function(folder) {
			var bits = folder.split(/:\\|\\/).map(function(x) { return document.createTextNode(x); });
			var end = bits.pop();

			var button = $("#Button_Folder");
			button.empty();
			bits.forEach(function(bit) {
				button.append(bit);
				button.append($("<span>❱</span>"));
			});
			button.append(end);

			Gui.scanning();
		},

		set_status: function(status, pct) {
			$("#Activity_Text").text(status);
			if (pct != null) {
				$("#Activity_Progress").val(pct);
			} else {
				$("#Activity_Progress").removeAttr("value");
			}
		},

		scanning: function() {
			Gui.reset_folder_summary();
			$("#Activity").show();
			$("#Analysis").show();

			$("#Button_Pause").show();
			$("#Button_Resume").hide();
			$("#Button_Stop").show();
			$("#Button_Analyse").hide();
			$("#Button_Compress").hide();
			$("#Command").show();
		},

		compacting: function() {
			$("#Button_Pause").show();
			$("#Button_Resume").hide();
			$("#Button_Stop").show();
			$("#Button_Analyse").hide();
			$("#Button_Compress").hide();
		},

		paused: function() {
			$("#Button_Pause").hide();
			$("#Button_Resume").show();
		},

		resumed: function() {
			$("#Button_Pause").show();
			$("#Button_Resume").hide();
		},

		stopped: function() {
			_flush_queued_updates();
			Gui.scanned();
		},

		scanned: function() {
			$("#Button_Pause").hide();
			$("#Button_Resume").hide();
			$("#Button_Stop").hide();
			$("#Button_Analyse").show();

			if ($("#File_Count_Compressible").text() != "0") {
				$("#Button_Compress").show();
			} else {
				$("#Button_Compress").hide();
			}
		},

		reset_folder_summary: function() {
			Gui.set_folder_summary({
				logical_size: 0,
				physical_size: 0,
				potential_savings_bytes: 0,
				compressed: {count: 0, logical_size: 0, physical_size: 0},
				compressible: {count: 0, logical_size: 0, physical_size: 0},
				skipped: {count: 0, logical_size: 0, physical_size: 0}
			});
		},

		set_folder_summary: function(data) {
			var logicalSize = data.logical_size || 0;
			var projectedOnDisk = data.projected_on_disk_size != null ? data.projected_on_disk_size : (data.physical_size || 0);
			var currentOnDisk = data.current_on_disk_size != null ? data.current_on_disk_size : logicalSize;
			var physicalSize = data.physical_size || projectedOnDisk;
			var savedBytes = Math.max(0, currentOnDisk - projectedOnDisk);
			var savedPct = logicalSize > 0 ? (savedBytes * 100.0 / logicalSize) : 0;
			var minSavingsPct = data.min_savings_percent != null ? data.min_savings_percent : parseFloat($("#Min_Savings").val() || 18);
			var savingsRatio = minSavingsPct > 0 ? (savedPct / minSavingsPct) : 999;

			$("#Size_Logical").text(Util.bytes_to_human(data.logical_size));
			$("#Size_Physical").text(Util.bytes_to_human(projectedOnDisk));
			$("#Estimate_From").text(Util.bytes_to_human(logicalSize));
			$("#Estimate_To").text(Util.bytes_to_human(projectedOnDisk));
			$("#Estimate_Current_On_Disk").text(Util.bytes_to_human(currentOnDisk));
			$("#Estimate_Recovery").text(Util.format_number(savedPct, 1) + "%");

			var estimateTo = $("#Estimate_To");
			var estimateRecovery = $("#Estimate_Recovery");
			estimateTo.removeClass("estimate-tone-low estimate-tone-good estimate-tone-great");
			estimateRecovery.removeClass("estimate-tone-low estimate-tone-good estimate-tone-great");
			var toneClass = "estimate-tone-low";
			if (savedPct < minSavingsPct || savingsRatio < 1.01) {
				toneClass = "estimate-tone-low";
			} else if (savingsRatio >= 2.0) {
				toneClass = "estimate-tone-great";
			} else {
				toneClass = "estimate-tone-good";
			}
			estimateTo.addClass(toneClass);
			estimateRecovery.addClass(toneClass);

			if (data.logical_size > 0) {
				var ratio = (projectedOnDisk / data.logical_size);
				$("#Compress_Ratio").text(Util.format_number(ratio, 2));
			} else {
				$("#Compress_Ratio").text("1.00");
			}

			if (data.logical_size > 0) {
				var total = data.logical_size;
				var compressedPhysical = data.compressed && data.compressed.physical_size ? data.compressed.physical_size : 0;
				var compressiblePhysical = data.compressible && data.compressible.physical_size ? data.compressible.physical_size : 0;
				var skippedPhysical = data.skipped && data.skipped.physical_size ? data.skipped.physical_size : 0;

				$("#Compressed_Size").text(Util.bytes_to_human(compressedPhysical));
				$("#Compressible_Size").text(Util.bytes_to_human(compressiblePhysical));
				$("#Skipped_Size").text(Util.bytes_to_human(skippedPhysical));

				document.getElementById("Breakdown_Compressed").style.width = "" + (100 * compressedPhysical / total).toFixed(2) + "%";
				document.getElementById("Breakdown_Compressible").style.width = "" + (100 * compressiblePhysical / total).toFixed(2) + "%";
				document.getElementById("Breakdown_Skipped").style.width = "" + (100 * skippedPhysical / total).toFixed(2) + "%";
			}

			var is_analysis = data.is_analysis !== undefined ? data.is_analysis : (data.compressible.count > 0 && data.compressed.count === 0);
			var potentialSavings = data.potential_savings_bytes;
			if (potentialSavings === undefined || potentialSavings === null) {
				potentialSavings = Math.max(0, (data.logical_size || 0) - (data.physical_size || 0));
			}

			if (is_analysis) {
				$("#Space_Saved").text(Util.bytes_to_human(potentialSavings));
			} else {
				$("#Space_Saved").text(Util.bytes_to_human(Math.max(0, (data.logical_size || 0) - projectedOnDisk)));
			}

			if (data.analysis_timing) {
				var t = data.analysis_timing;
				$("#Analysis_Timing").text(
					"Scan " + Util.format_number(t.combined_scan_seconds || 0, 2) + "s @ " + Util.format_number(t.scan_rate || 0, 0) + " files/sec"
					+ " | Entropy " + Util.format_number(t.entropy_seconds || 0, 2) + "s @ " + Util.format_number(t.entropy_rate || 0, 0) + " files/sec"
				);
			} else {
				$("#Analysis_Timing").text("");
			}

			$("#File_Count_Compressed").text(Util.format_number(data.compressed.count, 0));
			$("#File_Count_Compressible").text(Util.format_number(data.compressible.count, 0));
			$("#File_Count_Skipped").text(Util.format_number(data.skipped.count, 0));
		},

		analysis_complete: function() {
			$("#Activity").hide();
			$("#Analysis").show();
		},

		show_warning: function(title, message) {
			alert(title + "\n\n" + message);
		}
	};
})();

$(document).ready(Gui.boot);
