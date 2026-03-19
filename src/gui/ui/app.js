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

		quick_compression: function() {
			pywebview.api.get_quick_compression_targets().then(_dispatch_if_message);
		},

		start_quick_compression: function() {
			pywebview.api.start_quick_compression().then(_dispatch_if_message);
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
					Gui.queue_folder_summary(msg);
					break;

				case "QuickCompressionTargets":
					Gui.show_quick_mode(msg.directories || [], !!msg.allow_compactos);
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
	var current_summary_queue = null;
	var total_summary_queue = null;
	var directory_summary_queue = null;
	var directory_summary_history = [];
	var current_summary_state = null;
	var total_summary_state = null;
	var current_summary_directory = "";
	var total_summary_directory = "";
	var directory_summary_index = 0;
	var quick_history_mode = false;

	function _flush_queued_updates() {
		if (status_queue) {
			Gui.set_status(status_queue.status, status_queue.pct);
			status_queue = null;
		}
		if (current_summary_queue) {
			current_summary_state = current_summary_queue.data;
			current_summary_directory = current_summary_queue.directory || "";
			current_summary_queue = null;
		}
		if (total_summary_queue) {
			total_summary_state = total_summary_queue.data;
			total_summary_directory = total_summary_queue.directory || "";
			total_summary_queue = null;
		}
		if (directory_summary_queue) {
			directory_summary_history.push(directory_summary_queue);
			directory_summary_queue = null;
		}
		Gui.render_summaries();
	}

	return {
		request_initial_config: function() {
			var attempts = 0;
			var max_attempts = 60;

			function try_reset() {
				attempts += 1;
				if (window.pywebview && pywebview.api && typeof pywebview.api.reset_config === "function") {
					Action.reset_config();
					return;
				}
				if (attempts < max_attempts) {
					setTimeout(try_reset, 50);
				}
			}

			try_reset();
		},

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
			Gui.request_initial_config();
		},

		queue_status: function(status, pct) {
			status_queue = {
				status: status,
				pct: pct
			};
		},

		queue_folder_summary: function(data) {
			var directory = data.directory || "";
			var scope = data.scope || "";
			var summary = data.info || data;
			var payload = {
				data: summary,
				directory: directory
			};

			if (scope === "total") {
				total_summary_queue = payload;
			} else if (scope === "directory") {
				directory_summary_queue = payload;
			} else if (scope === "current") {
				current_summary_queue = payload;
			} else {
				current_summary_queue = payload;
				total_summary_queue = payload;
			}
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

		show_quick_mode: function(directories, allow_compactos) {
			var list = $("#Quick_Mode_Targets");
			var message = $("#Quick_Mode_Message");
			var startButton = $("#Button_Quick_Start");
			var note;

			list.empty();
			if (directories && directories.length) {
				directories.forEach(function(directory) {
					list.append($("<li></li>").text(directory));
				});
				note = "The backend found " + directories.length + " folders to analyze before any compression is started.";
				startButton.prop("disabled", false);
			} else {
				note = "No default quick-analysis targets were found on this system.";
				startButton.prop("disabled", true);
			}

			if (allow_compactos) {
				note += " Administrator privileges are available for CompactOS, but quick mode still only analyzes folders.";
			}

			message.text(note);

			$("#Quick_Mode").show();
		},

		hide_quick_mode: function() {
			$("#Quick_Mode").hide();
			$("#Quick_Mode_Targets").empty();
			$("#Quick_Mode_Message").text("");
			$("#Button_Quick_Start").prop("disabled", false);
		},

                set_min_savings: function(min_savings) {
			$("#Min_Savings").val(min_savings || 18);
		},

		set_folder: function(folder) {
			var button = $("#Button_Folder");
			button.text(folder);
			button.attr("title", folder);

			Gui.scanning();
		},

		set_status: function(status, pct) {
			$("#Activity_Text").text(status);
			if (pct != null) {
				$("#Activity_Progress").val(pct);
			} else {
				$("#Activity_Progress").removeAttr("value");
			}
			if (typeof status === "string" && status.indexOf("Quick analysis complete") === 0) {
				quick_history_mode = true;
				if (directory_summary_history.length) {
					directory_summary_index = directory_summary_history.length - 1;
				}
			}
		},

		scanning: function() {
			Gui.reset_folder_summary();
			Gui.hide_quick_mode();
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
			Gui.hide_quick_mode();
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
			Gui.hide_quick_mode();
			Gui.scanned();
		},

		scanned: function() {
			Gui.hide_quick_mode();
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
			current_summary_queue = null;
			total_summary_queue = null;
			directory_summary_queue = null;
			directory_summary_history = [];
			current_summary_state = null;
			total_summary_state = null;
			current_summary_directory = "";
			total_summary_directory = "";
			directory_summary_index = 0;
			quick_history_mode = false;
			Gui.set_folder_summary({
				logical_size: 0,
				physical_size: 0,
				potential_savings_bytes: 0,
				compressed: {count: 0, logical_size: 0, physical_size: 0},
				compressible: {count: 0, logical_size: 0, physical_size: 0},
				skipped: {count: 0, logical_size: 0, physical_size: 0}
			});
			Gui.set_current_folder_summary({
				logical_size: 0,
				physical_size: 0,
				potential_savings_bytes: 0,
				compressed: {count: 0, logical_size: 0, physical_size: 0},
				compressible: {count: 0, logical_size: 0, physical_size: 0},
				skipped: {count: 0, logical_size: 0, physical_size: 0}
			}, "");
		},

		render_summaries: function() {
			var current = current_summary_state || total_summary_state || {
				logical_size: 0,
				physical_size: 0,
				potential_savings_bytes: 0,
				compressed: {count: 0, logical_size: 0, physical_size: 0},
				compressible: {count: 0, logical_size: 0, physical_size: 0},
				skipped: {count: 0, logical_size: 0, physical_size: 0}
			};
			var total = total_summary_state || current_summary_state || current;
			if (quick_history_mode && directory_summary_history.length) {
				var selected = directory_summary_history[Math.max(0, Math.min(directory_summary_index, directory_summary_history.length - 1))];
				current = selected.data;
				current_summary_directory = selected.directory || current_summary_directory;
				Gui.update_directory_navigation();
			}

			Gui.set_folder_summary(total);
			Gui.set_current_folder_summary(current, current_summary_directory || total_summary_directory || "");
		},

		update_directory_navigation: function() {
			var nav = $("#Current_Directory_Nav");
			var label = $("#Current_Directory_Header_Text");
			var prev = $("#Directory_Prev");
			var next = $("#Directory_Next");
			var position = $("#Directory_Position");

			if (!quick_history_mode || !directory_summary_history.length) {
				nav.hide();
				label.show();
				return;
			}

			label.hide();
			nav.show();
			prev.prop("disabled", directory_summary_index <= 0);
			next.prop("disabled", directory_summary_index >= directory_summary_history.length - 1);
			position.text((directory_summary_index + 1) + " / " + directory_summary_history.length);
		},

		previous_directory: function() {
			if (!quick_history_mode || directory_summary_index <= 0) {
				return;
			}
			directory_summary_index -= 1;
			Gui.render_summaries();
		},

		next_directory: function() {
			if (!quick_history_mode || directory_summary_index >= directory_summary_history.length - 1) {
				return;
			}
			directory_summary_index += 1;
			Gui.render_summaries();
		},

		set_folder_summary: function(data) {
			var isAnalysis = data.is_analysis !== undefined ? data.is_analysis : (data.compressible.count > 0 && data.compressed.count === 0);
			var logicalSize = data.logical_size || 0;
			var projectedOnDisk = data.projected_on_disk_size != null ? data.projected_on_disk_size : (data.physical_size || 0);
			var currentOnDisk = data.current_on_disk_size != null ? data.current_on_disk_size : logicalSize;
			var currentDisplaySize = isAnalysis ? projectedOnDisk : currentOnDisk;
			var savedBytes = isAnalysis ? Math.max(0, currentOnDisk - projectedOnDisk) : Math.max(0, logicalSize - currentOnDisk);
			var savedPct = logicalSize > 0 ? (savedBytes * 100.0 / logicalSize) : 0;
			var minSavingsPct = data.min_savings_percent != null ? data.min_savings_percent : parseFloat($("#Min_Savings").val() || 18);
			var savingsRatio = minSavingsPct > 0 ? (savedPct / minSavingsPct) : 999;

			$("#Size_Logical").text(Util.bytes_to_human(data.logical_size));
			$("#Size_Physical").text(Util.bytes_to_human(currentDisplaySize));
			$("#Estimate_From").text(Util.bytes_to_human(logicalSize));
			$("#Estimate_To").text(Util.bytes_to_human(currentDisplaySize));
			$("#Estimate_Current_On_Disk").text(Util.bytes_to_human(currentOnDisk));
			$("#Estimate_Recovery").text(Util.format_number(savedPct, 1) + "%");
			$("#Estimate_Recovery_Label").text(isAnalysis ? "will be recovered" : "has been recovered");

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
				var ratio = (currentDisplaySize / data.logical_size);
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

			var potentialSavings = data.potential_savings_bytes;
			if (potentialSavings === undefined || potentialSavings === null) {
				potentialSavings = Math.max(0, (data.logical_size || 0) - (data.physical_size || 0));
			}

			if (isAnalysis) {
				$("#Space_Saved").text(Util.bytes_to_human(potentialSavings));
			} else {
				$("#Space_Saved").text(Util.bytes_to_human(Math.max(0, (data.logical_size || 0) - currentOnDisk)));
			}

			$("#Space_Saved_Label").text(isAnalysis ? "can be compressed in total" : "has been compressed in total");
			$("#Compressed_Size_Label").text(isAnalysis ? "already compressed" : "already compressed before run");
			$("#Compressible_Size_Label").text(isAnalysis ? "are compressible" : "compressed in this run");
			$("#Skipped_Size_Label").text("excluded");

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

		set_current_folder_summary: function(data, directory) {
			var isAnalysis = data.is_analysis !== undefined ? data.is_analysis : (data.compressible.count > 0 && data.compressed.count === 0);
			var logicalSize = data.logical_size || 0;
			var projectedOnDisk = data.projected_on_disk_size != null ? data.projected_on_disk_size : (data.physical_size || 0);
			var currentOnDisk = data.current_on_disk_size != null ? data.current_on_disk_size : logicalSize;
			var savedBytes = isAnalysis ? Math.max(0, currentOnDisk - projectedOnDisk) : Math.max(0, logicalSize - currentOnDisk);
			var savedPct = logicalSize > 0 ? (savedBytes * 100.0 / logicalSize) : 0;
			var minSavingsPct = data.min_savings_percent != null ? data.min_savings_percent : parseFloat($("#Min_Savings").val() || 18);
			var savingsRatio = minSavingsPct > 0 ? (savedPct / minSavingsPct) : 999;

			$("#Current_Directory_Name").text(directory || "Current Directory");
			Gui.update_directory_navigation();
			$("#Current_Estimate_From").text(Util.bytes_to_human(logicalSize));
			$("#Current_Estimate_To").text(Util.bytes_to_human(isAnalysis ? projectedOnDisk : currentOnDisk));
			$("#Current_Estimate_Current_On_Disk").text(Util.bytes_to_human(currentOnDisk));
			$("#Current_Estimate_Recovery").text(Util.format_number(savedPct, 1) + "%");
			$("#Current_Estimate_Recovery_Label").text(isAnalysis ? "will be recovered" : "has been recovered");

			var estimateTo = $("#Current_Estimate_To");
			var estimateRecovery = $("#Current_Estimate_Recovery");
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
