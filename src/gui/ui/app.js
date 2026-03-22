/* jshint strict: true, esversion: 5, browser: true */

	var I18n = (function()
	{
		"use strict";

		var payload = window.__TRASH_COMPACTOR_I18N__ || {};
		var translations = payload.translations || {};
		var locale = payload.locale || "en";

		function format_text(text, params) {
			if (!params) {
				return text;
			}

			return text.replace(/\{([a-zA-Z0-9_]+)\}/g, function(match, name) {
				if (Object.prototype.hasOwnProperty.call(params, name)) {
					return params[name];
				}
				return match;
			});
		}

		return {
			locale: locale,
			t: function(text, params) {
				return format_text(translations[text] || text, params);
			}
		};
	})();

	var bootConfig = window.__TRASH_COMPACTOR_BOOT_CONFIG__ || {};

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
				return number.toLocaleString(I18n.locale || "en", {minimumFractionDigits: digits, maximumFractionDigits: digits});
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
			if (Gui.should_restart_quick_analysis()) {
				Action.start_quick_compression();
				return;
			}
			if (Gui.is_busy() || Gui.has_started_analysis()) {
				return;
			}
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

function _dispatch_if_message(res) {
	if (res && typeof res === "object" && res.type) {
		Response.dispatch(res);
	}
}

// Responses come from Python
var Response = (function() {
	"use strict";

	return {
		dispatch: function(msg) {
			switch(msg.type) {
				case "Config":
										ignore_config_changes = true;
                                        Gui.set_decimal(msg.decimal);
                                        Gui.set_min_savings(msg.min_savings);
                                        Gui.set_checkbox("No_LZX", msg.no_lzx);
										$("#No_LZX_Help").text(msg.lzx_warning || default_lzx_help).toggleClass("warning", !!msg.lzx_warning);
                                        Gui.set_checkbox("Single_Worker", msg.single_worker);
										last_saved_config = Gui.get_config_payload();
										ignore_config_changes = false;
                                        break;

				case "Folder":
					Gui.set_folder(msg.path);
					break;

				case "Status":
						Gui.queue_status(msg.status, msg.pct, !!msg.quick_history);
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
					if (!Gui.should_restart_quick_analysis() && !Gui.is_busy() && !Gui.has_started_analysis()) {
						Gui.show_quick_mode(msg.directories || [], !!msg.allow_compactos);
					}
					break;

				case "ProgressUpdate":
						Gui.queue_status(msg.status, msg.pct, !!msg.quick_history);
					break;

				case "Warning":
					Gui.show_warning(msg.title, msg.message);
					break;

				case "Error":
						Gui.show_warning(I18n.t("Error"), msg.message || I18n.t("Unknown error"));
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
	var config_save_timer = null;
	var last_saved_config = null;
	var ignore_config_changes = false;
	var default_lzx_help = "";

	function _upsert_directory_summary(payload) {
		var i;
		for (i = 0; i < directory_summary_history.length; i++) {
			if (directory_summary_history[i].directory === payload.directory) {
				directory_summary_history[i] = payload;
				return;
			}
		}
		directory_summary_history.push(payload);
	}

	function _flush_queued_updates() {
		if (status_queue) {
			Gui.set_status(status_queue.status, status_queue.pct, status_queue.quick_history);
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
			_upsert_directory_summary(directory_summary_queue);
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

		apply_boot_config: function() {
			if (!bootConfig || bootConfig.type !== "Config") {
				return;
			}

			ignore_config_changes = true;
			Gui.set_decimal(bootConfig.decimal);
			Gui.set_min_savings(bootConfig.min_savings);
			Gui.set_checkbox("No_LZX", bootConfig.no_lzx);
			$("#No_LZX_Help").text(bootConfig.lzx_warning || default_lzx_help).toggleClass("warning", !!bootConfig.lzx_warning);
			Gui.set_checkbox("Single_Worker", bootConfig.single_worker);
			last_saved_config = Gui.get_config_payload();
			ignore_config_changes = false;
		},

		localize: function() {
			document.title = I18n.t("Trash Compactor GUI");
			$("#Button_Page_Compress").html("⌛ " + I18n.t("Compress"));
			$("#Button_Page_Settings").html("☸ " + I18n.t("Settings"));
			$("#Button_Page_About").html("⌕ " + I18n.t("About"));
			$("#Button_Quick").text(I18n.t("Quick compression"));
			$("#Quick_Action_Or").text(I18n.t("or"));
			$("#Button_Folder").text(I18n.t("Choose a folder"));
			$("#Quick_Mode .quick-mode-title").text(I18n.t("1-click mode"));
			$("#Quick_Mode .quick-mode-note").text(I18n.t("This runs the analysis first before anything is compressed."));
			$("#Button_Quick_Start").text(I18n.t("Start quick analysis"));
			$("#Button_Quick_Cancel").text(I18n.t("Cancel"));
			$("#Button_Pause").text("⏸️ " + I18n.t("Pause"));
			$("#Button_Resume").text("▶️ " + I18n.t("Resume"));
			$("#Button_Stop").text("⏹️ " + I18n.t("Stop"));
			$("#Button_Analyse").text("🔍 " + I18n.t("Analyse"));
			$("#Button_Compress").text("🗜 " + I18n.t("Compress"));
			$("#Current_Directory_Header_Text").text(I18n.t("Current Directory"));
			$("#Current_Directory_Name").text(I18n.t("Waiting for analysis..."));
			$("#Estimate_Recovery_Label, #Current_Estimate_Recovery_Label").text(I18n.t("will be recovered"));
			$("#Space_Saved_Label").text(I18n.t("can be compressed in total"));
			$("#Compressed_Size_Label").text(I18n.t("already compressed"));
			$("#Compressible_Size_Label").text(I18n.t("are compressible"));
			$("#Skipped_Size_Label").text(I18n.t("excluded"));
			$("#Quick_Estimate .total-card .estimate-card-title").text(I18n.t("Total"));
			$("#Current_Disk_Line strong, #Current_Directory_Disk_Line strong").text(I18n.t("Currently on-disk:"));
			$("#Placeholder_0B_1, #Placeholder_0B_2, #Placeholder_0B_3, #Placeholder_0B_4, #Placeholder_0B_5, #Placeholder_0B_6, #Placeholder_0B_7, #Placeholder_0B_8, #Placeholder_0B_9, #Placeholder_0B_10, #Placeholder_0B_11").text(I18n.t("0 B"));
			$("#Settings .settings-title").text(I18n.t("Compression settings"));
			$("#Settings .settings-note").text(I18n.t("These options decide how aggressively Trash Compactor searches for savings and how it behaves on slower drives."));
			$("#Button_Reset").text(I18n.t("Reset to recommended"));
			$("label[for='Min_Savings']").text(I18n.t("Minimum savings"));
			$("#Settings .setting-item").eq(0).find(".setting-help").text(I18n.t("Only files that can save at least this much space will be compressed. Higher values are more selective and faster; lower values squeeze out more space."));
			$("label[for='No_LZX'] .setting-label").text(I18n.t("Disable LZX compression"));
			default_lzx_help = I18n.t("Skips the strongest compression method, which can speed things up on systems where LZX would be too slow. Leave it enabled if you want the best balance of speed and savings.");
			$("#No_LZX_Help").text(default_lzx_help).removeClass("warning");
			$("label[for='Single_Worker'] .setting-label").text(I18n.t("Slow HDD mode"));
			$("label[for='Single_Worker'] + .setting-help").text(I18n.t("Limits the app to one worker at a time for older hard drives and noisy storage setups. On SSDs and modern drives, leaving it off usually gives better throughput."));
			$("label[for='SI_Units']").text(I18n.t("Units"));
			$("#SI_Units option[value='I']").text(I18n.t("Binary (MiB)"));
			$("#SI_Units option[value='D']").text(I18n.t("Decimal (MB)"));
			$("#Settings .setting-item:last-child .setting-help").text(I18n.t("Chooses how file sizes are displayed in the interface. This changes the labels you see, but not the compression results themselves."));
			$("#About h1").text(I18n.t("Trash Compactor GUI"));
			$("#About p strong").text(I18n.t("Where we're going, we don't need backups!"));
			$("#About p:nth-of-type(2)").html(I18n.t("Report problems to:") + " <a href=\"https://github.com/me-when-the-uh/trash-compactor\">https://github.com/me-when-the-uh/trash-compactor</a>.");
			$("#About p:nth-of-type(4)").text(I18n.t("An intelligent graphical interface for Windows NTFS filesystem compression using entropy-based heuristics."));
			$("#About p:nth-of-type(5)").text(I18n.t("For use with files and programs that rarely change - any file modifications will undo the compression for that file, so re-running this tool periodically is a good idea."));
			$("#About p:nth-of-type(6)").text(I18n.t("Vibe-coded in Python with a webview-slopped GUI."));
			$("#About p:nth-of-type(7)").html(I18n.t("GUI adapted from") + " <a href=\"https://github.com/Freaky/Compactor\">Compactor</a>");
		},

		boot: function() {
			Gui.localize();
			$("a[href]").on("click", function(e) {
				e.preventDefault();
				Action.open_url($(this).attr("href"));
				return false;
			});

			$("#Settings input, #Settings select").on("change input", function() {
				Gui.schedule_config_save();
			});

			$("#Button_Reset").on("click", function() {
				Action.reset_config();
			});

			setInterval(_flush_queued_updates, 100);
			Gui.apply_boot_config();
			Gui.request_initial_config();
		},

		queue_status: function(status, pct, quick_history) {
			status_queue = {
				status: status,
				pct: pct,
				quick_history: !!quick_history
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

		is_busy: function() {
			return $("#Button_Stop").is(":visible");
		},

		has_started_analysis: function() {
			return $("#Activity").is(":visible") || $("#Analysis").is(":visible");
		},

		should_restart_quick_analysis: function() {
			return quick_history_mode && !Gui.is_busy();
		},

		analyse: function() {
			if (Gui.should_restart_quick_analysis()) {
				Action.start_quick_compression();
				return;
			}
			if (Gui.is_busy()) {
				return;
			}
			Action.analyse();
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

		get_config_payload: function() {
			return {
				decimal: $("#SI_Units").val() == "D",
				min_savings: parseFloat($("#Min_Savings").val()),
				no_lzx: $("#No_LZX").is(":checked"),
				single_worker: $("#Single_Worker").is(":checked")
			};
		},

		schedule_config_save: function() {
			if (ignore_config_changes) {
				return;
			}

			if (config_save_timer) {
				clearTimeout(config_save_timer);
			}

			config_save_timer = setTimeout(function() {
				config_save_timer = null;
				var payload = Gui.get_config_payload();
				if (isNaN(payload.min_savings)) {
					return;
				}
				if (last_saved_config && JSON.stringify(payload) === JSON.stringify(last_saved_config)) {
					return;
				}
				last_saved_config = payload;
				Action.save_config(payload);
			}, 120);
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
				note = I18n.t("The program will analyse {count} folders for compressibility.", {count: directories.length});
				startButton.prop("disabled", false);
			} else {
				note = I18n.t("No default quick-analysis targets were found on this system.");
				startButton.prop("disabled", true);
			}

			if (allow_compactos) {
				note += " " + I18n.t("Administrator privileges are available, but they are not necessary for analysing folders.");
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
				$("#Min_Savings").val(min_savings != null ? min_savings : 18);
		},

		set_folder: function(folder) {
			var button = $("#Button_Folder");
			button.text(folder);
			button.attr("title", folder);

			Gui.scanning();
		},

		set_status: function(status, pct, quick_history) {
			$("#Activity_Text").text(status);
			if (pct != null) {
				$("#Activity_Progress").val(pct);
			} else {
				$("#Activity_Progress").removeAttr("value");
			}
			if (quick_history) {
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
			$("#Estimate_Recovery_Label").text(isAnalysis ? I18n.t("will be recovered") : I18n.t("has been recovered"));

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

			$("#Space_Saved_Label").text(isAnalysis ? I18n.t("can be compressed in total") : I18n.t("has been compressed in total"));
			$("#Space_Saved_Down_To").text(I18n.t("down to"));
			$("#Compressed_Size_Label").text(isAnalysis ? I18n.t("already compressed") : I18n.t("already compressed before run"));
			$("#File_Count_Compressed_Label, #File_Count_Compressible_Label, #File_Count_Skipped_Label").text(I18n.t("files"));
			$("#Compressible_Size_Label").text(isAnalysis ? I18n.t("are compressible") : I18n.t("compressed in this run"));
			$("#Skipped_Size_Label").text(I18n.t("excluded"));

			if (data.analysis_timing) {
				var t = data.analysis_timing;
				$("#Analysis_Timing").text(I18n.t(
					"Scan {scan_seconds}s @ {scan_rate} files/sec | Entropy {entropy_seconds}s @ {entropy_rate} files/sec",
					{
						scan_seconds: Util.format_number(t.combined_scan_seconds || 0, 2),
						scan_rate: Util.format_number(t.scan_rate || 0, 0),
						entropy_seconds: Util.format_number(t.entropy_seconds || 0, 2),
						entropy_rate: Util.format_number(t.entropy_rate || 0, 0)
					}
				));
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

			$("#Current_Directory_Name").text(directory || I18n.t("Current Directory"));
			Gui.update_directory_navigation();
			$("#Current_Estimate_From").text(Util.bytes_to_human(logicalSize));
			$("#Current_Estimate_To").text(Util.bytes_to_human(isAnalysis ? projectedOnDisk : currentOnDisk));
			$("#Current_Estimate_Current_On_Disk").text(Util.bytes_to_human(currentOnDisk));
			$("#Current_Estimate_Recovery").text(Util.format_number(savedPct, 1) + "%");
			$("#Current_Estimate_Recovery_Label").text(isAnalysis ? I18n.t("will be recovered") : I18n.t("has been recovered"));

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
