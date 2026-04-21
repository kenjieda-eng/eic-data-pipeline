const start = performance.now();
let getGraphDateStatus = false;
let graphTypeArr = [
 "spot_summary",
];
let csvDateArr = new Object;
csvDateArr["spot_summary"] = new Object;
csvDateArr["spot_index"] = new Object;
let spotDataArrCountMax;
let chart1;
let latestDateArr = [];
let minDateVal = new Date(2005, 4 - 1, 2);
let dateArr = [];
let dateArrAll = [];
let spotSummaryGraphAllData = new Object;
let spotSummaryAllData = new Object;
let spotSummaryAllDataYear = new Array();
let spotSummaryData = new Object;
spotSummaryData["price"] = new Object;
spotSummaryData["amount"] = new Object;
let spotIndexAllData = new Object;
let spotIndexDate = new Array();
let spotIndexDataArrCount = 0;
let price1Arr = new Object,price2Arr = new Object;
let price1All,price2All;
let spotDataArr = [];
let spotDataAll = [];
let spotDataArrCount = 0;
let setGraph = false;
let textLangArr = new Object;
let textArr = new Object;
textLangArr = {
	hokkaido : { 
		jp : "北海道",
		en : "Hokkaido"
	},
	tohoku : { 
		jp : "東北",
		en : "Tohoku"
	},
	tokyo : { 
		jp : "東京",
		en : "Tokyo"
	},
	chubu : { 
		jp : "中部",
		en : "Chubu"
	},
	hokuriku : { 
		jp : "北陸",
		en : "Hokuriku"
	},
	kansai : { 
		jp : "関西",
		en : "Kansai"
	},
	chugoku : { 
		jp : "中国",
		en : "Chugoku"
	},
	shikoku : { 
		jp : "四国",
		en : "Shikoku"
	},
	kyushu : { 
		jp : "九州",
		en : "Kyushu"
	},
	system_price : { 
		jp : "システムプライス",
		en : "System Price"
	},
	jpy : { 
		jp : "円",
		en : "JPY"
	},
	ymd : { 
		jp : "%Y年%m月%d日",
		en : "%d-%b-%Y"
	},

	ym : { 
		jp : "%Y年%m月",
		en : "%b-%Y"
	},
	y : { 
		jp : "%Y年",
		en : "%Y"
	},
	total_volume : { 
		jp : "約定総量",
		en : "Total Volume"
	},
	sell_volume : {
		jp : "売り入札量",
		en : "Sell Volume"
	},
	buy_volume : {
		jp : "買い入札量",
		en : "Buy Volume"
	},
	sell_block_bid : { 
		jp : "売りブロック入札量",
		en : "Block Sell Volume"
	},
	buy_block_bid : { 
		jp : "買いブロック入札量",
		en : "Block Buy Volume"
	},
	sell_block_all : { 
		jp : "売りブロック約定量",
		en : "Contracted Block Sell Volume"
	},
	buy_block_all : { 
		jp : "買いブロック約定量",
		en : "Contracted Block Buy Volume"
	},
	year : { 
		jp : "年度",
		en : ""
	},
	year2 : {
		jp : "年",
		en : ""
	},
	month : {
		jp : "月",
		en : ""
	},
	tooltip_format : { 
		jp : "{point.x:%Y/%m/%d %H:%M}",
		en : "{point.x:%d-%b-%Y %H:%M}"
	},
	date_format : { 
		jp : "%Y/%m/%d %H:%M",
		en : "%d-%b-%Y %H:%M"
	},
	date_format2 : { 
		jp : "%Y/%m/%d",
		en : "%d-%b-%Y"
	},
	date_format3 : { 
		jp : "-%Y/%m/%d",
		en : "-%d-%b-%Y"
	},
	date_format4 : { 
		jp : "%Y/%m",
		en : "%b-%Y"
	},
	date_format5 : { 
		jp : "-%Y/%m",
		en : "-%b-%Y"
	},
	date_format6 : { 
		jp : "%m/%d",
		en : "%d-%b"
	},
	date_format7 : { 
		jp : "-%m/%d",
		en : "-%d-%b"
	}
}

function convertMonth(num){
	if(num==1){
		return "Jan";
	}
	else if(num==2){
		return "Feb";
	}
	else if(num==3){
		return "Mar";
	}
	else if(num==4){
		return "Apr";
	}
	else if(num==5){
		return "May";
	}
	else if(num==6){
		return "Jun";
	}
	else if(num==7){
		return "Jul";
	}
	else if(num==8){
		return "Aug";
	}
	else if(num==9){
		return "Sep";
	}
	else if(num==10){
		return "Oct";
	}
	else if(num==11){
		return "Nov";
	}
	else if(num==12){
		return "Dec";
	}
	else{
		return "error";
	}
}

function deconvertMonth(month){
	if(month=="Jan"){
		return 1;
	}
	else if(month=="Feb"){
		return 2;
	}
	else if(month=="Mar"){
		return 3;
	}
	else if(month=="Apr"){
		return 4;
	}
	else if(month=="May"){
		return 5;
	}
	else if(month=="Jun"){
		return 6;
	}
	else if(month=="Jul"){
		return 7;
	}
	else if(month=="Aug"){
		return 8;
	}
	else if(month=="Sep"){
		return 9;
	}
	else if(month=="Oct"){
		return 10;
	}
	else if(month=="Nov"){
		return 11;
	}
	else if(month=="Dec"){
		return 12;
	}
	else{
		return "error";
	}
}

if($("html").hasClass("en-site"))
{
	Object.keys(textLangArr).forEach(function (key) {
		textArr[key] = textLangArr[key].en;
	});		
}
else
{
	$.datepicker.regional['ja'] = {
	  closeText: '閉じる',
	  prevText: '<前',
	  nextText: '次>',
	  currentText: '今日',
	  monthNames: ['1月','2月','3月','4月','5月','6月',
	  '7月','8月','9月','10月','11月','12月'],
	  monthNamesShort: ['1月','2月','3月','4月','5月','6月',
	  '7月','8月','9月','10月','11月','12月'],
	  dayNames: ['日曜日','月曜日','火曜日','水曜日','木曜日','金曜日','土曜日'],
	  dayNamesShort: ['日','月','火','水','木','金','土'],
	  dayNamesMin: ['日','月','火','水','木','金','土'],
	  weekHeader: '週',
	  dateFormat: 'yy/mm/dd',
	  firstDay: 0,
	  isRTL: false,
	  showMonthAfterYear: true,
	  yearSuffix: '年'};
	$.datepicker.setDefaults($.datepicker.regional['ja']);
	Object.keys(textLangArr).forEach(function (key) {
		textArr[key] = textLangArr[key].jp;
	});	
}
console.log(textArr);


let areaArrCount = 9;
let areaIdArr = {
	hokkaido:1,
	tohoku:2,
	tokyo:3,
	chubu:4,
	hokuriku:5,
	kansai:6,
	chugoku:7,
	shikoku:8,
	kyushu:9,
};
let areaArr = new Array();
areaArr = [
	{
		id:"hokkaido",
		name: textArr["hokkaido"],
		color:"#24B9FD"
	},
	{
		id:"tohoku",
		name: textArr["tohoku"],
		color:"#8B5FFC"
	},
	{
		id:"tokyo",
		name: textArr["tokyo"],
		color:"#125A7B"
	},
	{
		id:"chubu",
		name: textArr["chubu"],
		color:"#4A47C1"
	},
	{
		id:"hokuriku",
		name: textArr["hokuriku"],
		color:"#22CBBF"
	},
	{
		id:"kansai",
		name: textArr["kansai"],
		color:"#F270B5"
	},
	{
		id:"chugoku",
		name: textArr["chugoku"],
		color:"#D0177A"
	},
	{
		id:"shikoku",
		name: textArr["shikoku"],
		color:"#A31360"
	},
	{
		id:"kyushu",
		name: textArr["kyushu"],
		color:"#FD8D92"
	}
];

// Highchart全体設定
Highcharts.setOptions({
  global: {  // グローバルオプション
    useUTC: false   // GMTではなくJSTを使う
  },
  lang: {  // 言語設定
    rangeSelectorZoom: '',
    resetZoom: '表示期間をリセット',
    resetZoomTitle: '表示期間をリセット',
    rangeSelectorFrom: '',
    rangeSelectorTo: '〜',
    printButtonTitle: 'チャートを印刷',
    exportButtonTitle: '画像としてダウンロード',
    downloadJPEG: 'JPEG画像でダウンロード',
    downloadPDF: 'PDF文書でダウンロード',
    downloadPNG: 'PNG画像でダウンロード',
    downloadSVG: 'SVG形式でダウンロード',
    months: ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'],
    weekdays: ['日', '月', '火', '水', '木', '金', '土'],
    numericSymbols: null   // 1000を1kと表示しない
  }
});


let marginLeft = undefined;

if($("html").hasClass("windows-os"))
{
	marginLeft = 100;
}

function getGraphDate(dir,action,dlFile,delivery_date_time,selectYear,selectDate)
{
	let fileName = "/js/get_graph_year.php?dir=" + dir;
	var req = new XMLHttpRequest();
	req.open("get", fileName, true);
	req.setRequestHeader('Pragma', 'no-cache'); 
	req.setRequestHeader('Cache-Control', 'no-cache'); 
	req.setRequestHeader('If-Modified-Since', 'Thu, 01 Jun 1970 00:00:00 GMT');
	req.send(null);
	
	req.onload = function(){
		if(req.responseText){
			var getyear = req.responseText.split(",");
			csvDateArr[dir]["latestYear"] = getyear[0];
			csvDateArr[dir]["oldYear"] = getyear[1];
			csvDateArr[dir]["countMax"] = csvDateArr[dir]["latestYear"] - csvDateArr[dir]["oldYear"] + 1;
			if(dir == "spot_summary")
			{
				if(action == "download")
				{
					getGraphDate("spot_index","download",dlFile);
				}
				else if(action == "init")
				{
					let xAxisMax = delivery_date_time;
					let jsonArray = new Array();
					if(selectYear < csvDateArr["spot_summary"]["oldYear"])
					{
						selectYear = csvDateArr["spot_summary"]["oldYear"];
					}
					if(selectYear <= csvDateArr["spot_summary"]["latestYear"])
					{
						//let xAxisMin = 
						dateArr[selectYear] = new Array();
						getCSV("spot_summary",selectYear,"spot_summary",xAxisMax,"create",selectDate);
					}
					else
					{
						filterLock();
						return false;
					}
				}
			}
			if(dir == "spot_index")
			{
				if(action == "download")
				{
					SetDownload(dlFile);
				}
				else if(action == "init")
				{
					getGraphDateStatus = true;
					SetDownload();
				}
			}			
		}
	}
}

function filterLock()
{
	$(".filter-section__button-list .button").not(".active").addClass("disabled").prop("disabled",true);	
	$(".filter-section--lock").addClass("disabled");	
	$(".graph-section__content-wrapper").addClass("hide");
	$("#priceGraph").html("");
	$("#amountGraph").html("");
	$("#spotGraph1-table tbody").html("");
	$("#spotGraph2-table tbody").html("");
}


//csvの日付設定

function getCSV(file,fileDate,fileType,xAxisMax,dataStatus,selectDate){
	let fileName = "/js/csv_read.php?dir=" + file + "&file=" + file + "_" + fileDate + ".csv";
	var req = new XMLHttpRequest(); // HTTPでファイルを読み込むためのXMLHttpRrequestオブジェクトを生成
	req.open("get", fileName, true); // アクセスするファイルを指定
	req.setRequestHeader('Pragma', 'no-cache'); 
	req.setRequestHeader('Cache-Control', 'no-cache'); 
	req.setRequestHeader('If-Modified-Since', 'Thu, 01 Jun 1970 00:00:00 GMT');
	req.send(null); // HTTPリクエストの発行
	
	// レスポンスが返ってきたらconvertCSVtoArray()を呼ぶ
	req.onload = function(){
	  if(fileType == "spot_summary")
	  {
	 		csv2json(req.responseText,fileType,1,fileDate,file,xAxisMax,dataStatus,selectDate);    
	  }
  }
}

function csv2json(csvArray,fileType,period,fileDate,file,xAxisMax,dataStatus,selectDate){
	let jsonArray = [];
	let jsonData;
	let RowArray = csvArray.split('\n');
	RowArray.shift();//1行目を削除
	let items = RowArray[0].split(',');
	console.log(RowArray.length);
	if(fileType == "spot_summary")
	{
	  for(let i = 0; i < RowArray.length; i++){
	    let cellArray = RowArray[i].split(',');
	    let line = [];
	    if(cellArray[0] != undefined && cellArray[0] != "")
	    {
			
		    if(spotSummaryAllData[cellArray[0]] == undefined)
		    {
		      spotSummaryAllData[cellArray[0]] = new Object;
		    }
		    spotSummaryAllData[cellArray[0]][cellArray[1]] = new Array();
		    let date = new Date(cellArray[0]);
		    //console.log(date);
		    //console.log(cellArray);
		    let csv_date_unixtime = date.getTime() + ((Number(cellArray[1]) - 1)*1800*1000);
		    //console.log(new Date(csv_date_unixtime));
		    line[0] = csv_date_unixtime;
				if(period == "all")
				{
		    	dateArrAll.push(line[0]);
				}
				else
				{
					dateArrAll.push(line[0]);
					dateArr[fileDate].push(line[0]);
				}
				if(fileDate == csvDateArr["spot_summary"]["latestYear"]) //最新のデータの日付
				{
					latestDateArr.push(line[0]);
				}
		    for(let j = 2; j < (items.length); j++){
		      	line[j] = cellArray[j];
		    }
				jsonArray.push(line);
				spotSummaryAllData[cellArray[0]][cellArray[1]].push(line);
			}
		}
		spotSummaryGraphAllData[fileDate] = jsonArray;
		if(fileDate == csvDateArr["spot_summary"]["latestYear"])
		{
			let latestDate = Number(moment(Math.max.apply(null, latestDateArr)/1000,'X').format('YYYYMMDD'));
			let selectDateNumBefore = new Date(selectDate).toISOString();
			let selectDateNum = Number(moment(selectDateNumBefore).format('YYYYMMDD'));
			if(latestDate >= selectDateNum)
			{
				setGrafhPrice(spotSummaryGraphAllData[fileDate],fileDate,xAxisMax,dataStatus,selectDate);			
			}
			else
			{
				filterLock();
				return false;
			}
		}
		else
		{
			setGrafhPrice(spotSummaryGraphAllData[fileDate],fileDate,xAxisMax,dataStatus,selectDate);	
		}
	}
}

function utc2dateString(utc_msec) {
  d=new Date();
  d.setTime(utc_msec);
  return d.getFullYear()+'/'+(d.getMonth()+1)+'/'+d.getDate();
}

function graphSetCheck(file)
{
	setGraph = true;
	for (let year = csvDateArr[file]["oldYear"]; year <= csvDateArr[file]["latestYear"]; year++) {
	 	if(!$("#spotGraph1_" + year).find(".highcharts-container").length)
	 	{
	 		setGraph = false; 		
	 	}
	 	if(!$("#spotGraph2_" + year).find(".highcharts-container").length)
	 	{
			setGraph = false; 	
	 	}
	}
}

function compareFunc(a, b) {
  return b - a;
}

function checkShowCell($input)
{
	if($input.prop("checked") == false)
	{
		return "hide";
	}
	else
	{
		return "";
	}
}

function checkInputGraph($input)
{
	if($input.prop("checked") == false)
	{
		return false;
	}
	else
	{
		return true;
	}
}

function setTableCell(cellData)
{
	if(Number(cellData))
	{
		cellData = Number(cellData).toLocaleString()		
	}
	return cellData;
}

function setNumberCheck(number)
{
	if(Number(number))
	{
		number = Number(number);	
	}
	else
	{
		number = null;
	}
	return number;
}

function SetDownload(dlFile)
{
	//DLデータセット
	let selectHtmlArr = new Object;
	selectHtmlArr["spot_summary"] = "";
	selectHtmlArr["spot_index"] = "";
	for (let year = csvDateArr["spot_summary"]["latestYear"]; year >= csvDateArr["spot_summary"]["oldYear"]; year--) {
		selectHtmlArr["spot_summary"] += '<option value="spot_summary_' + year + '.csv">'  + year + textArr["year"] + '</option>';
	}
	for (let year = csvDateArr["spot_index"]["latestYear"]; year >= csvDateArr["spot_index"]["oldYear"]; year--) {
		selectHtmlArr["spot_index"] += '<option value="spot_index_' + year + '.csv">'  + year + textArr["year"] + '</option>';
	}
	$("#dl-select--spot_summary").html(selectHtmlArr["spot_summary"]);
	$("#dl-select--spot_index").html(selectHtmlArr["spot_index"]);
	$("#data-area__filter .dl-button").removeClass("hide init");
	if(dlFile != undefined)
	{
		$(".overlay,#modal-box--" + dlFile).fadeIn("fast");
	}
}

function setGrafhCalendar()
{		 
	/*-----------------------------------------
	Calender
	-------------------------------------------*/
	$("#datepicker").datepicker({
		changeMonth: true,
		changeYear: true,
		minDate: minDateVal,
		yearRange: '2005:+1',
	  onSelect: function(dateText, inst){
		 let selectYear;
		 let trading_date  = new Date(dateText);
		 let delivery_date = new Date(trading_date.getFullYear(), trading_date.getMonth(), trading_date.getDate(), 23,30);
		 let delivery_date_time = delivery_date.getTime();
		 let dataMinDate;
		 $("#datepicker-area").fadeOut();	
		 console.log(dateText);
		 let selectDate;
		 if($("html").hasClass("en-site"))
		 {
			 let selectDateBefore = new Date(dateText).toISOString();
			 selectDate = moment(selectDateBefore).format('YYYY/MM/DD');
		 }
		 else
		 {
			 selectDate = dateText;
		 }
		 let ymdArr = selectDate.split('/');
		 $("#datepicker--date__year").text(ymdArr[0]);
			if($("html").hasClass("en-site")){
				$("#datepicker--date__month").text(convertMonth(ymdArr[1]));
			}
			else{
				$("#datepicker--date__month").text(ymdArr[1]);
			}
		 $("#datepicker--date__day").text(ymdArr[2]);
		 
		 $("#button--calender-show").hide();
		 $("#datepicker--date").removeClass("hide");
		 
		 if(ymdArr[1] < 4)//1〜3月の場合
		 {
			 selectYear = Number(ymdArr[0] - 1);
		 }
		 else
		 {
		 	selectYear = ymdArr[0];
		 }
		 if(price1Arr[selectYear] == undefined)
		 {
			$("#priceGraph").html("");
			$("#amountGraph").html("");
			$("#spotGraph1-table tbody").html("");
			$("#spotGraph2-table tbody").html("");
			$(".graph-section__content-wrap").removeClass("hide");
			if(!getGraphDateStatus)
		  {
			  getGraphDate("spot_summary","init",undefined,delivery_date_time,selectYear,selectDate);
		  }
		  else
		  {
			  if(selectYear > csvDateArr["spot_summary"]["latestYear"])
				{
					filterLock();
				}
				else
				{
					let xAxisMax = delivery_date_time;
					let jsonArray = new Array();
					if(selectYear < csvDateArr["spot_summary"]["oldYear"])
					{
						selectYear = csvDateArr["spot_summary"]["oldYear"];
					}
					//let xAxisMin = 
					dateArr[selectYear] = new Array();
					getCSV("spot_summary",selectYear,"spot_summary",xAxisMax,"create",selectDate);
				}
		  }
		 }
		 else
		 {
				let latestDate = Number(moment(Math.max.apply(null, latestDateArr)/1000,'X').format('YYYYMMDD'));
				let selectDateNumBefore = new Date(selectDate).toISOString();
				let selectDateNum = Number(moment(selectDateNumBefore).format('YYYYMMDD'));
				if(selectYear == csvDateArr["spot_summary"]["latestYear"] && latestDate < selectDateNum)
				{
					filterLock();
				}
				else
				{
					graphFilterSet();
					$(".graph-section__content-wrap").removeClass("hide");
				 dataMinDate = Math.min.apply(null, dateArr[selectYear]);
				  //表の値変更
				 let htmlArr =  new Object;
				 htmlArr["price"] = "";
				 for (let i = 1; i <= 48; i++){
						let spotTime = moment(spotSummaryAllData[selectDate][i][0][0]/1000,'X').format('HH:mm');
						let csv_next_unixtime = spotSummaryAllData[selectDate][i][0][0] + (1800*1000);
						let spotNextTime = moment(csv_next_unixtime/1000,'X').format('HH:mm');
						//約定価格
					  htmlArr["price"] += "<tr>";
						htmlArr["price"] += "<td>" + spotTime + "-" + spotNextTime + "</td>";
						for (let j = 5; j <= 14; j++){
							let areaClass;
							let tdAreaClass = "";
							let $inputArea = "";
							if(j == 5)
							{
								areaClass = "system_price";	
								$inputArea = $("#system_price--table");
							}
							else
							{
								areaClass = areaArr[j-6].id;	
								tdAreaClass = "table-cell--area";
								$inputArea = $("#area_" + areaClass + "--table");
							}
							htmlArr["price"] += "<td class='table-cell " + tdAreaClass + " cell--number cell--" + areaClass + " " + checkShowCell($inputArea) + "'>" + Number(spotSummaryAllData[selectDate][i][0][j]).toFixed(2) + "</td>";
						}
						htmlArr["price"] += "</tr>";
						//入札・約定量
						htmlArr["amount"] += "<tr>";
						htmlArr["amount"] += "<td>" + spotTime + "-" + spotNextTime + "</td>";
						htmlArr["amount"] += "<td class='cell--number cell--amount_all " + checkShowCell($("#amount_all--table")) + "'>" + setTableCell(spotSummaryAllData[selectDate][i][0][4]) + "</td>";//約定総量
						htmlArr["amount"] += "<td class='cell--number cell--amount_bid_sell " + checkShowCell($("#amount_sell--table")) + "'>" + setTableCell(spotSummaryAllData[selectDate][i][0][2]) + "</td>";//売り入札量
						htmlArr["amount"] += "<td class='cell--number cell--amount_bid_buy " + checkShowCell($("#amount_buy--table")) + "'>" + setTableCell(spotSummaryAllData[selectDate][i][0][3]) + "</td>";//買い入札量
						htmlArr["amount"] += "<td class='cell--number cell--amount_sell_block_bid " + checkShowCell($("#amount_sell_block_bid--table")) + "'>" +  setTableCell(spotSummaryAllData[selectDate][i][0][15]) + "</td>";//売りブロック入札総量
						htmlArr["amount"] += "<td class='cell--number cell--amount_sell_block_all " + checkShowCell($("#amount_sell_block_all--table")) + "'>" + setTableCell(spotSummaryAllData[selectDate][i][0][16]) + "</td>";//売りブロック約定総量
						htmlArr["amount"] += "<td class='cell--number cell--amount_buy_block_bid " + checkShowCell($("#amount_buy_block_bid--table")) + "'>" + setTableCell(spotSummaryAllData[selectDate][i][0][17]) + "</td>";//買いブロック入札総量
						htmlArr["amount"] += "<td class='cell--number cell--amount_buy_block_all " + checkShowCell($("#amount_buy_block_all--table")) + "'>" + setTableCell(spotSummaryAllData[selectDate][i][0][18]) + "</td>";//買いブロック約定総量
				htmlArr["amount"] += "</tr>";
					}
					$("#spotGraph1-table .graph-section__table tbody").html(htmlArr["price"]);
					$("#spotGraph2-table .graph-section__table tbody").html(htmlArr["amount"]);
				 let dates = dateText.split('/');
				 let trading_date_all;
				 	switch ($("#filter-section--period input:checked").val()) {
					  case "day":
					    selectedPeriod = 1;
					    break
					  case "month":
					    selectedPeriod = 2;
					    break
					  case "year":
					    selectedPeriod = 3;
					    break
					  case "5year":
					    selectedPeriod = 4;
					    break
					  default:
							selectedPeriod = 1;    
					}
				 
				 switch (selectedPeriod) {
					 case 2: 
					 		if(compareMonthDay(selectDate))
					 		{
						 		trading_date_all = new Date(trading_date.getFullYear(), trading_date.getMonth(), 1);
					 		}
					 		else
					 		{
						 		trading_date_all = new Date(trading_date.getFullYear(), trading_date.getMonth() - 1, trading_date.getDate(), trading_date.getHours());
					 		}
					 	break
					 case 3: 
						 	trading_date_all = new Date(trading_date.getFullYear() - 1, trading_date.getMonth(), trading_date.getDate(), trading_date.getHours());
					 	break
					 case 4: 
					 		trading_date_all = new Date(trading_date.getFullYear() - 5, trading_date.getMonth(), trading_date.getDate(), trading_date.getHours());
					 	break
					 default:
							trading_date_all = trading_date;
				 }
				
				 let trading_date_time = trading_date_all.getTime();		 
				 if(trading_date_time < dataMinDate)
				 {
					 trading_date_time = dataMinDate;
				 }		 
				 let jsonArray = spotSummaryGraphAllData[selectYear];
				 if($("#priceGraph_" + selectYear).length)
				 {
					 price1Arr[selectYear].xAxis[0].setExtremes(trading_date_time,delivery_date_time);
					 price2Arr[selectYear].xAxis[0].setExtremes(trading_date_time,delivery_date_time);
					 price1Arr[selectYear].redraw();
					 price2Arr[selectYear].redraw();	
				 }
				 else
				 {
					 let xAxisMax = delivery_date_time;
					 setGrafhPrice(jsonArray,selectYear,xAxisMax,"update",selectDate);	 		 	
				 }
			 }
			 
					 
			 //インデックス値変更
	/*
			 let indexDate = selectDate;
			 let ttv = Number(spotIndexAllData[indexDate][4]).toLocaleString() + "<span class='data-unit'>kWh</span>";
			 $("#filter-section--index__date").html(spotIndexAllData[indexDate][0]);
			 $("#da-24").html(parseInt(spotIndexAllData[indexDate][1]) + "<span class='data-unit'>" + setDecimal(spotIndexAllData[indexDate][1]) + textArr["jpy"]  + "/kWh</span>");
			 $("#ttv").html(ttv);
			 $("#da-dt").html(parseInt(spotIndexAllData[indexDate][2]) + "<span class='data-unit'>" + setDecimal(spotIndexAllData[indexDate][2]) + textArr["jpy"]  + "/kWh</span>");
			 $("#da-pt").html(parseInt(spotIndexAllData[indexDate][3]) + "<span class='data-unit'>" + setDecimal(spotIndexAllData[indexDate][3]) + textArr["jpy"]  + "/kWh</span>");
	*/			 
			}		
		}
  });	
	$("#datepicker--date").on("click",function(){
		$("#datepicker-area").fadeToggle();	
	});
}

function getAverageData(arrayData)
{
	let total = arrayData.reduce(function(sum, element){
		if(element)
		{
			return sum + element;		
		}
	}, 0);		
	let average = total/arrayData.length;
	return Math.round(average);
}

function graphFilterSet()
{
	let type = $("#filter-section--type .button.active").data("type");
	$(".filter-section__button-list .button").removeClass("disabled").prop("disabled",false);	
	if(type == "graph")
	{
		$(".filter-section").removeClass("disabled");	
	}
	else
	{
		$("#filter-section--detail").removeClass("disabled");	
	}
	$(".graph-section__content-wrapper").removeClass("hide");
}

//選択月と前月の日付を比較
function compareMonthDay(selectDate)
{
	let date = new Date(selectDate);
	let lastMonthDay = getLastMonthDay(selectDate);
	if(date.getDate() > lastMonthDay.getDate())
	{
		return true;
	}
	else
	{
		return false;
	}
}

//選択した日付の月の日数
function getLastDay(selectDate)
{
	let date = new Date(selectDate);
	let year = date.getFullYear();
	let month = date.getMonth() + 1;
	let lastDay = new Date(year,month,0);
	return lastDay;
}

//選択した日付の前月の日数
function getLastMonthDay(selectDate)
{
	let date = new Date(selectDate);
	let year = date.getFullYear();
	let month = date.getMonth();
	let lastMonthDay = new Date(year,month,0);
	return lastMonthDay;
}


//月の日数を時間に変換
function setMonthTime(selectDate)
{
	let date = new Date(selectDate);
	let lastDay = getLastDay(selectDate);
	let lastMonthDay = getLastMonthDay(selectDate);	
	let dateCount;
	let dayDiff;
	if(lastDay.getDate() == lastMonthDay.getDate())
	{
		dateCount = ((lastDay.getDate()+1)*24) - 0.5;	
	}
	else if(lastDay.getDate() > lastMonthDay.getDate())
	{
		if(date.getDate() > lastMonthDay.getDate())
		{
			dayDiff = date.getDate() - lastMonthDay.getDate();
			dateCount = (lastMonthDay.getDate() + dayDiff)*24 - 0.5;	
		}
		else
		{
			dayDiff = lastMonthDay.getDate() - lastDay.getDate() + 1;
			dateCount = (lastDay.getDate() + dayDiff)*24 - 0.5;	
		}
	}
	else
	{
		dayDiff = lastMonthDay.getDate() - lastDay.getDate() + 1;
		dateCount = (lastDay.getDate() + dayDiff)*24 - 0.5;	
	}
	return dateCount;
}

function setGrafhPrice(jsonArray,fileDate,xAxisMax,dataStatus,selectDate)
{	
	if(!getGraphDateStatus)
  {
	  getGraphDate("spot_index","init");
  }
  
  graphFilterSet();
  
	let xAxisMaxVal = undefined;
	let navMinVal = Math.min.apply(null, dateArr[fileDate]);
	let xAxisMinVal = navMinVal;
	let style;
	let marker;
	let graphSelectedPeriod;
	let selectDateVal = selectDate;
	let startDate;
	let rangeMonth = {
	    type: 'month',
	    count: 1,
	    text: '1m',
	    title: 'View 1 month'
	};
	
	switch ($("#filter-section--period input:checked").val()) {
	  case "day":
	    graphSelectedPeriod = 0;
	    marker = true;
	    break
	  case "month":
	  	let trading_date = new Date(selectDate);
	  	let lastDayBefore = new Date(selectDate);
	  	let lastDay = new Date(lastDayBefore.getFullYear(), lastDayBefore.getMonth() + 1, 0);
	 	 	trading_date = new Date(trading_date.getFullYear(), trading_date.getMonth() - 1, trading_date.getDate());
	 	 	xAxisMinVal = trading_date.getTime();
/*
	 	 	if(lastDayBefore.getTime() == lastDay.getTime())
	  	{
		  	trading_date = new Date(lastDayBefore.getFullYear(), lastDayBefore.getMonth(),1);
	  	}
*/
	    graphSelectedPeriod = 1;
	    marker = false;
	    break
	  case "year":
	    graphSelectedPeriod = 2;
	    marker = false;
	    break
	  default:
			graphSelectedPeriod = 0;    
			marker = false;
	}
	
	if(xAxisMax)
	{
		xAxisMaxVal = xAxisMax;
	}
	
	console.log("jsonArray",jsonArray);
	let htmlGraphArr = new Object;
	htmlGraphArr["price"] = '<div id="priceGraph_' + fileDate + '" class="period1YGraph priceGraph spotGraph_' + fileDate + '" ' + style + '>';
	htmlGraphArr["amount"] = '<div id="amountGraph_' + fileDate + '" class="period1YGraph amountGraph spotGraph_' + fileDate + '" ' + style + '>';
	$("#priceGraph").html(htmlGraphArr["price"]);
	$("#amountGraph").html(htmlGraphArr["amount"]);
	
	spotSummaryAllDataYear.push(Number(fileDate));
	//spotSummaryAllData[fileDate] = jsonArray;
	const spot_summary = new Object;	
	spot_summary["price"] = new Object;	
	spot_summary["price"]["system_price"] = jsonArray.map(x => [x[0], setNumberCheck(x[5])]);//システムプライス
	spot_summary["price"]["hokkaido"] = jsonArray.map(x => [x[0], setNumberCheck(x[6])]);//北海道"
	spot_summary["price"]["tohoku"] = jsonArray.map(x => [x[0], setNumberCheck(x[7])]);//東北
	spot_summary["price"]["tokyo"] = jsonArray.map(x => [x[0], setNumberCheck(x[8])]);//東京
	spot_summary["price"]["chubu"] = jsonArray.map(x => [x[0], setNumberCheck(x[9])]);//中部
	spot_summary["price"]["hokuriku"] = jsonArray.map(x => [x[0], setNumberCheck(x[10])]);//北陸
	spot_summary["price"]["kansai"] = jsonArray.map(x => [x[0], setNumberCheck(x[11])]);//関西
	spot_summary["price"]["chugoku"] = jsonArray.map(x => [x[0], setNumberCheck(x[12])]);//中国
	spot_summary["price"]["shikoku"] = jsonArray.map(x => [x[0], setNumberCheck(x[13])]);//四国
	spot_summary["price"]["kyushu"] = jsonArray.map(x => [x[0], setNumberCheck(x[14])]);//九州	
	spot_summary["price"]["series"] = new Array();
	spot_summary["price"]["series"] = [
		{
		  data: spot_summary["price"]["system_price"],
		  id: "system_price",
		  color: "#277AB4",
		  zIndex: 9,
		  name: textArr["system_price"],
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#system_price")),
/*
		  dataGrouping: {
          enabled: false
        }
*/
		},
		{
		  data: spot_summary["price"]["hokkaido"],
		  visible: false,
		  id: areaArr[0].id,
		  color: areaArr[0].color,
		  zIndex: 0,
		  name: areaArr[0].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_hokkaido"))
		},
		{
		  data: spot_summary["price"]["tohoku"],
		  visible: false,
		  id: areaArr[1].id,
		  color: areaArr[1].color,
		  zIndex: 1,
		  name: areaArr[1].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_tohoku"))
		},
		{
		  data: spot_summary["price"]["tokyo"],
		  visible: false,
		  id: areaArr[2].id,
		  color: areaArr[2].color,
		   zIndex: 2,
		  name: areaArr[2].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_tokyo"))
		},
		{
		  data: spot_summary["price"]["chubu"],
		  visible: false,
		  id: areaArr[3].id,
		  color: areaArr[3].color,
		  zIndex: 3,
		  name: areaArr[3].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_chubu"))
		},
		{
		  data: spot_summary["price"]["hokuriku"],
		  visible: false,
		  id: areaArr[4].id,
		  color: areaArr[4].color,
		  zIndex: 4,
		  name: areaArr[4].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_hokuriku"))
		},
		{
		  data: spot_summary["price"]["kansai"],
		  visible: false,
		  id: areaArr[5].id,
		  color: areaArr[5].color,
		  zIndex: 5,
		  name: areaArr[5].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_kansai"))
		},
		{
		  data: spot_summary["price"]["chugoku"],
		  visible: false,
		  id: areaArr[6].id,
		  color: areaArr[6].color,
		  zIndex: 6,
		  name: areaArr[6].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_chugoku"))
		},
		{
		  data: spot_summary["price"]["shikoku"],
		  visible: false,
		  id: areaArr[7].id,
		  color: areaArr[7].color,
		  zIndex: 7,
		  name: areaArr[7].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_shikoku"))
		},
		{
		  data: spot_summary["price"]["kyushu"],
		  visible: false,
		  id: areaArr[8].id,
		  color: areaArr[8].color,
		  zIndex: 8,
		  name: areaArr[8].name,
		  lineWidth: 1,
		  marker: {
		    radius: 4,
		    symbol: "square"
		  },
		  visible: checkInputGraph($("#area_kyushu"))
		},
	];
	
	console.log(spot_summary["price"]["series"]);
	console.log(xAxisMinVal);
	
  // Create the chart
  price1Arr[fileDate] = Highcharts.stockChart('priceGraph_' + fileDate , {
	  chart: {
			 height: 600,
			 marginLeft: marginLeft
		},
		credits: {
        enabled: false
    },
    plotOptions: {
        series: {
            marker: {
                enabled: marker
            },
            dataGrouping: {
           	 dateTimeLabelFormats: {
               millisecond: [textArr["date_format"], textArr["date_format"], '-%H:%M'],
               second: [textArr["date_format"], textArr["date_format"], '-%H:%M'],
               minute: [textArr["date_format"], textArr["date_format"], '-%H:%M'],
               hour: [textArr["date_format"], textArr["date_format"], '-%H:%M'],
               day: [textArr["date_format2"], textArr["date_format2"], textArr["date_format3"]],
               week: [textArr["date_format2"], textArr["date_format2"], textArr["date_format3"]],
               month: ['%B %Y', '%B', '-%B %Y'],
               year: ['%Y', '%Y', '-%Y'],
               all: ['%Y', '%Y', '-%Y']
            }
          }
        }
    },
    yAxis: {
	    labels: {
	      style: {
	          color: 'black',
	          fontFamily: 'Oswald',
	      }
      },
	    title: {
	     text: '(' + textArr["jpy"] + '/kWh)',
	     align: 'high',
	     offset: 0,
	     rotation: 0,
       y: -10,
       x:25,
       style: {
           color: 'black'
       }
		  },
			opposite: false
	  },
	  navigator: {
	     xAxis: {
			 	 max: undefined,
		     min: navMinVal,
            dateTimeLabelFormats: {
		          day:  textArr["date_format4"],
		          week: textArr["date_format4"],
		          month:textArr["date_format4"],
		          year: textArr["date_format4"],
		          all: textArr["date_format4"]
		        },
            labels: {
				      style: {
				          color: 'black',
				          fontFamily: 'Oswald',
				      }
			      },
        }
	  },
	  xAxis: {
			max: xAxisMaxVal,
			min: xAxisMinVal,
			tickPosition: 'inside',
		  labels: {
	      style: {
	          color: 'black',
	          fontFamily: 'Oswald',
	      },	  
				formatter: function() {
					console.log(this);
	        return Highcharts.dateFormat(this.dateTimeLabelFormat, this.value);
      	},	  
      },
		  dateTimeLabelFormats: {
         hour: ['%H:%M', '%H:%M', '-%H:%M'],
				 day: [textArr["date_format6"], textArr["date_format6"], textArr["date_format7"]],
				 month: [textArr["date_format2"], textArr["date_format2"], textArr["date_format3"]],
				 year: ['%Y', '%Y', '-%Y']
		  },
		  tickPositioner: function (min, max) {
          var xDataRange = max - min,
              positions = [],
              tick = min;

          positions.info = {higherRanks: {}};

          if (xDataRange <= 86400000) {
              // If range is 1 day max => 1 tick every hour
              increment = 3600000;
              positions.info.unitName = "hour";
              $("#spotGraph1 .period1YGraph .highcharts-markers").show();
          } else if (xDataRange > 86400000 && xDataRange <= 2678400000 + (86400000)) {
              // If range is between 1 day and 1 month => 1 tick every day
              increment = 86400000;
              positions.info.unitName = "day";
              $("#spotGraph1 .period1YGraph .highcharts-markers").show();
          } else if (xDataRange > 2678400000 + (86400000) && xDataRange <= 31536000000) {
              // If range is between 1 month and 1 year => 1 tick every month
              increment = 2678400000;
              positions.info.unitName = "month";
              $("#spotGraph1 .period1YGraph .highcharts-markers").hide();
          } else {
              // If more than 1 year displayed, 1 tick every year
              increment = 365 * 24 * 60 * 60 * 1000;
              positions.info.unitName = "year";
              //$("#spotGraph1 .period1YGraph .highcharts-markers").hide();
          }
					
					let period = $("#filter-section--period input:checked").val();
          // Create ticks
          for(tick = min; tick - increment < max; tick += increment) {
              if(window.matchMedia('screen and (max-width:1600px)').matches)
	            {
	              if(tick == min)
	              {
		            	positions.push(tick);  
	              }
	              if(period == "year" && tick >= max - increment)
	              {
		            	positions.push(max);  
	              }
								else if(tick < max && tick >= max - increment)
	              {
		            	positions.push(tick);  
	              }
	            }
              else
              {
	              positions.push(tick);
              }
          }
          positions.info.totalRange = positions[positions.length - 1] - positions[0];
          return positions;
      }
	  },
		tooltip: { 
			headerFormat: textArr["tooltip_format"],
			pointFormat:  '<span style="color:{series.color}">{series.name}</span>: <b>{point.y:,.2f}(' + textArr["jpy"] + '/kWh)</b><br/>',
			//xDateFormat: textArr["date_format"]
		},
    rangeSelector: {
	    inputDateFormat: textArr["ymd"],
			inputEditDateFormat: textArr["ymd"],
      allButtonsEnabled: true,
      buttons: [
      {
		    type: 'hour',
		    count: 23.5,
		    text: '1d',
		    title: 'View 1 day'
		}, 
		{
		    type: 'hour',
		    count: setMonthTime(selectDate),
		    text: '1m',
		    title: 'View 1 month'
		}
		,{
		    type: 'year',
		    count: 1,
		    text: '1y',
		    title: 'View 1 year'
		}, 
		{
		    type: 'all',
		    text: 'All',
		    title: 'View all'
		}],
			labelStyle: {
	        color: '#277AB4',
	        fontFamily: 'メイリオ,sans-serif',
	    },
      selected: graphSelectedPeriod
    },
    legend: {
      enabled: true,
      align: 'right',
			verticalAlign: 'top',
			itemStyle: {
          color: 'black',
          fontFamily: 'メイリオ,sans-serif',
          fontWeight: 400,
      }
    },
    series: spot_summary["price"]["series"],
    time: {
      timezoneOffset: (new Date).getTimezoneOffset()
    },
    responsive: {
        rules: [
        	{
	        	condition: {
                maxWidth: 960
            },
					  chartOptions: {
/*
						  xAxis: {
							  showLastLabel: true,
							  tickPosition: 'inside',
								dateTimeLabelFormats: {
				         hour: ['%H', '%H', '-%H'],
				         month: [textArr["date_format2"], textArr["date_format2"], textArr["date_format3"]],
						  	}, 
							},
*/
							tooltip: { 
								style: {
									fontSize: 8
				        }
							},
						}
        	},
        	{
            condition: {
                maxWidth: 767
            },
	            chartOptions: {
		            chart: {
									 height: 600,
									 marginTop: 25,
								},

	             plotOptions: {
				        series: {
				            marker: {
				                enabled: false
				            },
				          }
						    },
/*
						    
						     xAxis: {
									labels: {
								      style: {
								          fontSize: 10,
								      }	      
							      },
								},
*/
			           rangeSelector: {
							    inputPosition: {
							        align: 'left',
							        x: 0,
							        y: -20
							    },
							    buttonPosition: {
							        align: 'right',
							        x: 0,
							        y: 0
							    },
								},
                legend: {
	                	verticalAlign: 'bottom',
                    align: 'left',
                    itemStyle: {
						          color: 'black',
						          fontFamily: 'メイリオ,sans-serif',
											fontSize: '10px',
							      }
                }
            }
        }]
    }
/*
    title: {
      text: '約定価格（kWh）'
    }
*/
  });

  spot_summary["amount"] = new Object;	
  spot_summary["amount"]["sell"] = jsonArray.map(x => [x[0], setNumberCheck(x[2])]);
  spot_summary["amount"]["buy"] = jsonArray.map(x => [x[0], setNumberCheck(x[3])]);
  spot_summary["amount"]["all"] = jsonArray.map(x => [x[0], setNumberCheck(x[4])]);
  spot_summary["amount"]["sell_block_bid"] = jsonArray.map(x => [x[0], setNumberCheck(x[15])]);
  spot_summary["amount"]["sell_block_all"] = jsonArray.map(x => [x[0], setNumberCheck(x[16])]);
  spot_summary["amount"]["buy_block_bid"] = jsonArray.map(x => [x[0], setNumberCheck(x[17])]);
  spot_summary["amount"]["buy_block_all"] = jsonArray.map(x => [x[0], setNumberCheck(x[18])]);
  // Create the chart
  Highcharts.setOptions({
		lang: {
	  	thousandsSep: ','
	  }
	});
	
	let pointFormatVal = '<span style="color:{series.color}">{series.name}</span>: <b>{point.y}(kWh)</b><br/>';
	if(window.matchMedia('screen and (max-width:1023px)').matches)
	{
		pointFormatVal = '<span style="color:{series.color}">{series.name}</span><br><b>{point.y}(kWh)</b><br/>';
	}
	
	price2Arr[fileDate] = Highcharts.stockChart('amountGraph_' + fileDate, {
     navigator: {
        adaptToUpdatedData: false,
    },
    credits: {
        enabled: false
    },
    chart: {
        type: 'column',
        height: 550,
    },
    plotOptions: {
      column: {
          pointPadding: 0.2,
          borderWidth: 0
      }
		},
    yAxis: { 
	    title: {
		     text: '(kWh)',
		     align: 'high',
		     offset: 0,
		     rotation: 0,
         y: -10 
		  },
		  opposite: false
	  },
	  navigator: {
	     xAxis: {
					 	max: undefined,
					 	min: xAxisMinVal,
            dateTimeLabelFormats: {
		          day:  textArr["date_format4"],
		          week: textArr["date_format4"],
		          month:textArr["date_format4"],
		          year: textArr["date_format4"]
		        },
            labels: {
				      style: {
				          color: 'black',
				          fontFamily: 'Oswald',
				      }
			      },
        }
	  },
	  xAxis: {
			max: xAxisMaxVal,
			min: xAxisMinVal,
		  labels: {
	      style: {
	          color: 'black',
	          fontFamily: 'Oswald',
	      },
	      formatter: function() {
					console.log(this);
	        return Highcharts.dateFormat(this.dateTimeLabelFormat, this.value);
      	},		      
      },
		  dateTimeLabelFormats: {
         hour: ['%H:%M', '%H:%M', '-%H:%M'],
				 day: [textArr["date_format6"], textArr["date_format6"], textArr["date_format7"]],
				 month: [textArr["date_format2"], textArr["date_format2"], textArr["date_format3"]],
		  },
		  tickPositioner: function (min, max) {
          var xDataRange = max - min,
              positions = [],
              tick = min;

          positions.info = {higherRanks: {}};

          if (xDataRange <= 86400000) {
              // If range is 1 day max => 1 tick every hour
              increment = 3600000;
              positions.info.unitName = "hour";
              //$("#spotGraph1 .highcharts-markers").show();
          } else if (xDataRange > 86400000 && xDataRange <= 2678400000 + (86400000)) {
              // If range is between 1 day and 1 month => 1 tick every day
              increment = 86400000;
              positions.info.unitName = "day";
              //$("#spotGraph1 .highcharts-markers").hide();
          } else if (xDataRange > 2678400000 + (86400000) && xDataRange <= 31536000000) {
              // If range is between 1 month and 1 year => 1 tick every month
              increment = 2678400000;
              positions.info.unitName = "month";
              //$("#spotGraph1 .highcharts-markers").hide();
          } else {
              // If more than 1 year displayed, 1 tick every year
              increment = 365 * 24 * 60 * 60 * 1000;
              positions.info.unitName = "year";
              //$("#spotGraph1 .highcharts-markers").hide();
          }
					
					let period = $("#filter-section--period input:checked").val();
          // Create ticks
          for(tick = min; tick - increment < max; tick += increment) {
	          if(window.matchMedia('screen and (max-width:1600px)').matches)
            {
              if(tick == min)
              {
	            	positions.push(tick);  
              }
              if(period == "year" && tick >= max - increment)
              {
	            	positions.push(max);  
              }
							else if(tick < max && tick >= max - increment)
              {
	            	positions.push(tick);  
              }
            }
            else
            {
              positions.push(tick);
            }
          }
          positions.info.totalRange = positions[positions.length - 1] - positions[0];
          return positions;
      }
	  },
		tooltip: { headerFormat: textArr["tooltip_format"] ,pointFormat: pointFormatVal},
    rangeSelector: {
	    inputDateFormat: textArr["ymd"],
			inputEditDateFormat: textArr["ymd"],
      allButtonsEnabled: true,
      buttons: [ 
      {
		    type: 'hour',
		    count: 23.5,
		    text: '1d',
		    title: 'View 1 day'
		}, 
      {
		    type: 'hour',
		    count: setMonthTime(selectDate),
		    text: '1m',
		    title: 'View 1 month'
		}, {
		    type: 'year',
		    count: 1,
		    text: '1y',
		    title: 'View 1 year'
		}, 
		{
		    type: 'all',
		    text: 'All',
		    title: 'View all'
		}],
      selected: graphSelectedPeriod
    },
    legend: {
      enabled: true,
      align: 'right',
			verticalAlign: 'top',
			itemStyle: {
          color: 'black',
          fontFamily: 'メイリオ,sans-serif',
          fontWeight: 400,
      }
    },
    series: [
	    {
        data: spot_summary["amount"]["all"],
        id: 'amount_all',
        name: textArr["total_volume"],
        lineWidth: 1,
        color: '#939393',
        visible: checkInputGraph($("#amount_all")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
	    {
        data: spot_summary["amount"]["sell"],
        id: 'sell',
        name: textArr["sell_volume"],
        lineWidth: 1,
        color: '#4A47C1',
        visible: checkInputGraph($("#amount_sell")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
      {
        data: spot_summary["amount"]["buy"],
        id: 'buy',
        name: textArr["buy_volume"],
        lineWidth: 1,
        color: '#F270B5',
        visible: checkInputGraph($("#amount_buy")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
      {
        data: spot_summary["amount"]["sell_block_bid"] ,
        id: 'sell_block_bid',
        name: textArr["sell_block_bid"],
        lineWidth: 1,
        color: '#1BB9FD',
        visible: checkInputGraph($("#amount_sell_block_bid")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
      {
        data: spot_summary["amount"]["buy_block_bid"],
        id: 'buy_block_bid',
        name: textArr["buy_block_bid"],
        lineWidth: 1,
        color: '#D0177A',
        visible: checkInputGraph($("#amount_buy_block_bid")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
      {
        data: spot_summary["amount"]["sell_block_all"] ,
        id: 'sell_block_all',
        name: textArr["sell_block_all"],
        lineWidth: 1,
        color: '#22CBBF',
        visible: checkInputGraph($("#amount_sell_block_all")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
      {
        data: spot_summary["amount"]["buy_block_all"],
        id: 'buy_block_all',
        name: textArr["buy_block_all"],
        lineWidth: 1,
        color: '#FD8D92',
        visible: checkInputGraph($("#amount_buy_block_all")),
        dataGrouping: {
          approximation: function (groupData) {
	          let average = getAverageData(groupData);
						return average;
          }
        }
      },
    ],	      
    time: {
      timezoneOffset: (new Date).getTimezoneOffset()
    },
    responsive: {
        rules: [
        		{
		        	condition: {
	                maxWidth: 960
	            },
						  chartOptions: {
							  plotOptions: {
						      column: {
						          pointPadding: 0.1,
						      }
								},
								tooltip: { 
									style: {
										fontSize: 8
					        }
								},
/*
							  xAxis: {
									showLastLabel: true,
									dateTimeLabelFormats: {
					         hour: ['%H', '%H', '-%H'],
					         month: [textArr["date_format2"], textArr["date_format2"], textArr["date_format3"]],
							  	}, 
								}
*/
							}
	        	},
        		{
            condition: {
                maxWidth: 767
            },
	            chartOptions: {
		             chart: {
									 height: 600,
									 marginTop: 25,
								},
/*
		             xAxis: {
									labels: {
								      style: {
								          fontSize: 10,
								      }	      
							      },
								},
*/
					      yAxis: {
								  labels: {
							      style: {
							          fontSize: 10,
							      }	      
						      }
					      },
		            rangeSelector: {
							    inputPosition: {
							        align: 'left',
							        x: -10,
							        y: -20
							    },
							    buttonPosition: {
							        align: 'right',
							        x: 0,
							        y: 0
							    },
								},
                legend: {
	                	verticalAlign: 'bottom',
                    align: 'left',
                    itemStyle: {
						          color: 'black',
						          fontFamily: 'メイリオ,sans-serif',
											fontSize: 10,
							      }
                }
            }
        }]
    }
  });
  
  const end2 = performance.now();
	console.log("spot_2021:" + (end2 - start) + "ms");
	
	console.log(latestDateArr);
	
	if(dataStatus == "create")
	{
		let htmlArr =  new Object;
		if(selectDateVal)
		{
			
		}
		else
		{
			selectDateVal = moment(Math.max.apply(null, dateArr[fileDate])/1000,'X').format('YYYY/MM/DD');
		}
			htmlArr["price"] = "";
			htmlArr["amount"] = "";
			for (let i = 1; i <= 48; i++){
				let spotTime = moment(spotSummaryAllData[selectDateVal][i][0][0]/1000,'X').format('HH:mm');
				let csv_next_unixtime = spotSummaryAllData[selectDateVal][i][0][0] + (1800*1000);
				let spotNextTime = moment(csv_next_unixtime/1000,'X').format('HH:mm');
			  //約定価格
			  htmlArr["price"] += "<tr>";
				htmlArr["price"] += "<td>" + spotTime + "-" + spotNextTime + "</td>";
				for (let j = 5; j <= 14; j++){
					let areaClass;
					let tdAreaClass = "";
					let $inputArea = "";
					if(j == 5)
					{
						areaClass = "system_price";	
						$inputArea = $("#system_price--table");
					}
					else
					{
						areaClass = areaArr[j-6].id;	
						tdAreaClass = "table-cell--area";
						$inputArea = $("#area_" + areaClass + "--table");
					}
					htmlArr["price"] += "<td class='table-cell " + tdAreaClass + " cell--number cell--" + areaClass + " " + checkShowCell($inputArea) + "'>" + Number(spotSummaryAllData[selectDateVal][i][0][j]).toFixed(2) + "</td>";
				}
				htmlArr["price"] += "</tr>";
				//入札・約定量
				htmlArr["amount"] += "<tr>";
				htmlArr["amount"] += "<td>" + spotTime + "-" + spotNextTime + "</td>";
				htmlArr["amount"] += "<td class='cell--number cell--amount_all " + checkShowCell($("#amount_all--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][4]) + "</td>";//約定総量
				htmlArr["amount"] += "<td class='cell--number cell--amount_bid_sell " + checkShowCell($("#amount_sell--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][2]) + "</td>";//売り入札量
				htmlArr["amount"] += "<td class='cell--number cell--amount_bid_buy " + checkShowCell($("#amount_buy--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][3]) + "</td>";//買い入札量
				htmlArr["amount"] += "<td class='cell--number cell--amount_sell_block_bid " + checkShowCell($("#amount_sell_block_bid--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][15]) + "</td>";//売りブロック入札総量
				htmlArr["amount"] += "<td class='cell--number cell--amount_sell_block_all " + checkShowCell($("#amount_sell_block_all--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][16]) + "</td>";//売りブロック約定総量
				htmlArr["amount"] += "<td class='cell--number cell--amount_buy_block_bid " + checkShowCell($("#amount_buy_block_bid--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][17]) + "</td>";//買いブロック入札総量
				htmlArr["amount"] += "<td class='cell--number cell--amount_buy_block_all " + checkShowCell($("#amount_buy_block_all--table")) + "'>" + setTableCell(spotSummaryAllData[selectDateVal][i][0][18]) + "</td>";//買いブロック約定総量
				htmlArr["amount"] += "</tr>";
			}
			$("#spotGraph1-table .graph-section__table tbody").html(htmlArr["price"]);
			$("#spotGraph2-table .graph-section__table tbody").html(htmlArr["amount"]);
		}
	
	
		//let minDate = Math.min.apply(null, dateArr);
				
	
  //price2.series[0].hide();
	
	//チャートの初期化
/*
var chartPrice = Chart('highchartsPrice', {
        chart: { height: 350 },
        title: { text: 'システムプライス' },
        legend: { x: -190 },
        yAxis: { title: { text: '(円/kWh)' }},
        tooltip: { pointFormat: '<span style="color:{series.color}">{series.name}</span>: <b>{point.y:,.2f}(円/kWh)</b><br/>' }

    }, onLoadChart),
chartExecution = Chart('highchartsExecution', {
        chart: { type: 'column', height: 300 },
        title: { text: '約定総量' },
        legend: { x: -210 },
        // yAxis: { title: { text: '(kWh)' }, tickInterval : 10000, labels : {formatter: function() { return this.value / 1000 + 'k'; }}},
        yAxis: { title: { text: '(kWh)' }},
        tooltip: { valueSuffix: 'kWh' }
    }, onLoadChart),
    $datepicker = $('#datepicker');
	
*/
}

function setDecimal(num)
{
	num = Number(num).toFixed(2);
	let decimal = String(num).split(".")[1];
	if(decimal != undefined)
	{
		decimal = "." + decimal;
		
	}
	else
	{
		decimal = ".00";
	}
	return decimal;
}

/*
function showIndex()
{
	spotIndexDate.sort(compareFunc);
	let beforeDate = moment(spotIndexDate[0],'YYYYMMDD');
	let indexDate = moment(beforeDate).format('YYYY/MM/DD');
	let ttv = Number(spotIndexAllData[indexDate][4]).toLocaleString() + "<span class='data-unit'>kWh</span>";
	$("#filter-section--index__date").text(spotIndexAllData[indexDate][0]);
	$("#da-24").html(parseInt(spotIndexAllData[indexDate][1]) + "<span class='data-unit'>" + setDecimal(spotIndexAllData[indexDate][1]) + textArr["jpy"] + "/kWh</span>");
	$("#ttv").html(ttv);
	$("#da-dt").html(parseInt(spotIndexAllData[indexDate][2]) + "<span class='data-unit'>" + setDecimal(spotIndexAllData[indexDate][2]) + textArr["jpy"] + "/kWh</span>");
	$("#da-pt").html(parseInt(spotIndexAllData[indexDate][3]) + "<span class='data-unit'>" + setDecimal(spotIndexAllData[indexDate][3]) + textArr["jpy"] + "/kWh</span>");
}
*/

/*
function init()
{
	//最新のspotデータ
	dateArr[csvDateArr["spot_summary"]["latestYear"]] = new Array();
  getCSV("spot_summary",csvDateArr["spot_summary"]["latestYear"],"spot_summary",undefined,"create");//最新のcsvデータ表示
  if(!getGraphDateStatus)
  {
	  getGraphDate("spot_index","init");
  }
}
*/

/* インデックス */
/*
function setIndex()
{
	//最新のインデックスデータ
	getCSV("spot_index",csvDateArr["spot_index"]["latestYear"],"spot_index");
	for (let year = csvDateArr["spot_index"]["oldYear"]; year <= csvDateArr["spot_index"]["latestYear"]; year++) {
	 	if(year < csvDateArr["spot_index"]["latestYear"])
	 	{
	 		getCSV("spot_index",year,"spot_index");//過去のインデックスデータ表示
	 	}
	}
}
*/

function getSelectYear()
{
	let selectDate = $('#datepicker').datepicker('getDate');
  let selectYear = selectDate.getFullYear();
  if((selectDate.getMonth() + 1) < 4) //1〜3月の場合
  {
	  selectYear = 	selectDate.getFullYear() - 1;	
  }
  return selectYear;
}

$(function(){
	 setGrafhCalendar();
	
	 let updatedOptions = new Object;
	
	 /* 表示期間変更 */
	 $("#filter-section--period input").on("change",function(){		 
		 let period = $(this).val();
		 let $periodLabel = $(this).parent(".filter-label");
		 let selectedPeriod;
		 let marker;
		 
		 if($(this).prop("checked") == true)
		 {
			 switch (period) {
				  case "day":
				  	marker = true;
				    selectedPeriod = 0;
				    break
				  case "month":
					  marker = false;
				    selectedPeriod = 1;
				    break
				  case "year":
					  marker = false;
				    selectedPeriod = 2;
				    break
				  case "5year":
				  	marker = false;
				    selectedPeriod = 3;
				    break
				  default:
				  	marker = true;
						selectedPeriod = 1;    
				}
				
				updatedOptions["price"] = {
					plotOptions: {
	        	series: {
	            marker: {
	                enabled: marker
	            },
	          }
			    },
				 	rangeSelector: {
						 selected: selectedPeriod
					}	
				}
				
				updatedOptions["amount"] = {
				 	rangeSelector: {
						 selected: selectedPeriod
					}	
				}
				
				let yyyy;
				$("#filter-section--period .filter-label").not($periodLabel).removeClass("active");
				$periodLabel.addClass("active");
				let trading_date = $('#datepicker').datepicker('getDate');
				let selectDate = moment(trading_date).format('YYYY/MM/DD');
				let trading_date_time = trading_date.getTime();
				let delivery_date = new Date(trading_date.getFullYear(), trading_date.getMonth(), trading_date.getDate(), 23,30);
				console.log(delivery_date);
				let delivery_date_time = delivery_date.getTime();		
				let minDate = Math.min.apply(null, dateArr[trading_date.getFullYear()]);
				
				yyyy = delivery_date.getFullYear();
				if((delivery_date.getMonth() + 1) < 4)//1〜3月の場合
				{
					minDate = Math.min.apply(null, dateArr[trading_date.getFullYear() - 1]);
					yyyy = 	delivery_date.getFullYear() - 1;	 
				}
				
				switch (period) {
				  case "day":
				    selectedPeriod = 0;
				    break
				  case "month":
				    selectedPeriod = 1;
				    if(compareMonthDay(selectDate))
				    {
					    trading_date = new Date(trading_date.getFullYear(), trading_date.getMonth(), 1);
				    }
				    else
				    {
					    trading_date = new Date(trading_date.getFullYear(), trading_date.getMonth() - 1, trading_date.getDate(), trading_date.getHours());
				    }
				    //trading_date.setMonth(trading_date.getMonth() - 1);
				    trading_date_time = trading_date.getTime();
				    break
				  case "year":
				    selectedPeriod = 2;
				    trading_date = new Date(trading_date.getFullYear() - 1, trading_date.getMonth(), trading_date.getDate(), trading_date.getHours());
				    trading_date_time = trading_date.getTime();
				    break
				  default:
						selectedPeriod = 1;    
				}
				
				if(trading_date_time < minDate)
				{
					 trading_date_time = minDate;	
				}
				
				console.log("delivery_date_time",delivery_date_time);
				price1Arr[yyyy].update(updatedOptions["price"], true, true);	
				price2Arr[yyyy].update(updatedOptions["amount"], true, true);	
				price1Arr[yyyy].xAxis[0].setExtremes(trading_date_time,delivery_date_time);
				price2Arr[yyyy].xAxis[0].setExtremes(trading_date_time,delivery_date_time);
				price1Arr[yyyy].redraw();
				price2Arr[yyyy].redraw();
			}
	 });
  
  /*約定価格変更グラフ用 */
  $("#checkbox-area--graph #system_price").on("change",function(){  
	  let selectYear = getSelectYear();	  
	  if($(this).prop("checked") == true) {
		  let id = 0;		
			price1Arr[selectYear].series[0].setVisible(true,true);	
	  } 
	  else
	  {
			price1Arr[selectYear].series[0].setVisible(false,true);	
	  }
  });
  $("#checkbox-area--graph .checkbox-list #area_all").on("change",function(){	   
	  let selectYear = getSelectYear();	
	  if($("#area_all").prop("checked") == true) {
			$("#checkbox-area--graph .checkbox-list input").not(this).prop("checked",true);	
			for (let step = 0; step < areaArrCount; step++) {
				price1Arr[selectYear].series[step + 1].setVisible(true,true);	
			}
	  } 
	  else
	  {
			$("#checkbox-area--graph .checkbox-list input").not(this).prop("checked",false);
			for (let step = 0; step < areaArrCount; step++) {
				price1Arr[selectYear].series[step + 1].setVisible(false,true);	
			}
	  }
  });
  $("#checkbox-area--graph .checkbox-list input").not("#area_all").on("change",function(){	  
	  let selectYear = getSelectYear();	
	  let areaCheckCount;
		areaCheckCount = $("#checkbox-area--graph .checkbox-list input:checked").not("#area_all").length;	  
	  if($(this).prop("checked") == true) {
		  let id = areaIdArr[$(this).val()];		
			price1Arr[selectYear].series[id].setVisible(true,true);
			if(areaCheckCount == 9)
		  {
				$("#checkbox-area--graph .checkbox-list #area_all").prop("checked",true);
		  }	
	  } 
	  else
	  {
		  $("#checkbox-area--graph .checkbox-list #area_all").prop("checked",false);
			let id = areaIdArr[$(this).val()];		
			price1Arr[selectYear].series[id].setVisible(false,true);
	  }
  });
  
  /*約定価格変更テーブル用 */
  $("#checkbox-area--table #system_price--table").on("change",function(){
	  if($(this).prop("checked") == true) {
			$("#spotGraph1-table .cell--system_price").removeClass("hide");
	  } 
	  else
	  {
			$("#spotGraph1-table .cell--system_price").addClass("hide");
	  }
  });
  
  $("#checkbox-area--table .checkbox-list #area_all--table").on("change",function(){
		if($(this).prop("checked") == true) {
			$("#checkbox-area--table .checkbox-list input").not(this).prop("checked",true);
			$("#cell--area-header").prop("colspan",9);
			$("#spotGraph1-table .table-cell--area").removeClass("hide");
			$("#spotGraph1-table .cell--system_price").removeClass("area-hide");
	  } 
	  else
	  {
		  $("#checkbox-area--table .checkbox-list input").not(this).prop("checked",false);
		  $("#spotGraph1-table .table-cell--area").addClass("hide");
			$("#spotGraph1-table .cell--system_price").addClass("area-hide");
	  }
	});
	
	$("#checkbox-area--table .checkbox-list input").not("#area_all--table").on("change",function(){
		let areaCheckCount;
		areaCheckCount = $("#checkbox-area--table .checkbox-list input:checked").not("#area_all--table").length;
		let id = $(this).val();
		if($(this).prop("checked") == true) {
			$("#spotGraph1-table .cell--system_price").removeClass("area-hide");
			$("#spotGraph1-table .cell--area-header").removeClass("hide");
			$("#spotGraph1-table .cell--" + id).removeClass("hide");
			$("#cell--area-header").prop("colspan",areaCheckCount);
			if(areaCheckCount == 9)
		  {
				$("#checkbox-area--table .checkbox-list #area_all--table").prop("checked",true);
		  }
	  } 
	  else
	  {
		  $("#spotGraph1-table .cell--" + id).addClass("hide");
		  $("#cell--area-header").prop("colspan",areaCheckCount);
		  $("#checkbox-area--table .checkbox-list #area_all--table").prop("checked",false);
		  if(areaCheckCount == 0)
		  {
			  $("#spotGraph1-table .cell--system_price").addClass("area-hide");
				$("#spotGraph1-table .table-cell--area").addClass("hide");
		  }
	  }
	});
  
  /* 入札・約定量変更グラフ用 */
  $("#checkbox-amount--graph .checkbox-list input").on("change",function(){
	  let id =$(this).val();
	  let selectYear = getSelectYear();	
	  if($(this).prop("checked") == true) {
			price2Arr[selectYear].series[id].setVisible(true,true);	  
		} 
	  else
	  {
			price2Arr[selectYear].series[id].setVisible(false,true);	
		} 	  
  });
  
  /* 入札・約定量変更テーブル用 */
  $("#checkbox-amount--table .checkbox-list input").on("change",function(){
	  let id =$(this).val();
	  let cell = $(this).data("cell");
	  let th = $(this).data("th");
	  if($(this).prop("checked") == true) {
		  let colspan = $("#spotGraph2-table .cell--" + th).prop("colspan");
		  if($("#spotGraph2-table .cell--" + th).hasClass("hide")){
				$("#spotGraph2-table .cell--" + th).removeClass("hide");
				$("#spotGraph2-table .cell--" + th).prop("colspan",1);
		  }
		  else
		  {
			  $("#spotGraph2-table .cell--" + th).prop("colspan",colspan + 1);
		  }
		  $("#spotGraph2-table .cell--" + cell).removeClass("hide");	  
		} 
	  else
	  {
		  let colspan = $("#spotGraph2-table .cell--" + th).prop("colspan");
		  $("#spotGraph2-table .cell--" + th).prop("colspan",colspan - 1);
		  $("#spotGraph2-table .cell--" + cell).addClass("hide");
		  if(colspan - 1 == 0){
				$("#spotGraph2-table .cell--" + th).addClass("hide");
		  }
		} 	  
  });
  
  /* 表示タイプ */
  $("#filter-section--type .button").on("click",function(){
		let type = $( this).data("type");
		$(this).addClass("active");
		$("#filter-section--type .button").not(this).removeClass("active");
		if(type == "graph")
		{
			$("#spotGraph1").show();
			$("#spotGraph1-table").hide();  
			$("#spotGraph2").show();
			$("#spotGraph2-table").hide();
			$("#filter-section--period").removeClass("disabled"); 
			$(".checkbox-area--graph").addClass("active");
			$(".checkbox-area--table").removeClass("active");
		}
		else
		{
			$("#spotGraph1").hide();
			$("#spotGraph1-table").show();
			$("#spotGraph2").hide();
			$("#spotGraph2-table").show();
			$("#filter-section--period").addClass("disabled"); 
			$(".checkbox-area--graph").removeClass("active");
			$(".checkbox-area--table").addClass("active");
		}
  });
  
  /* 受渡月 */
  $("#month-select").on("click",function(){
		$("#month-list").slideToggle();
		$(this).toggleClass("is-open");  
  });
  
  $(document).on("click","#month-list a",function(){
		let year = $(this).data("year");
		let current = $("#month-select .year").text();
		if(year != current)
		{
			setMonthGraph($(this).data("year"));
		}
		$("#month-select").removeClass("is-open");  
		$("#month-list").slideUp();
	});
	
	/* 受渡年 */
  $("#year-select").on("click",function(){
		$("#year-list").slideToggle();
		$(this).toggleClass("is-open");  
  });
  
  $(document).on("click","#year-list a",function(){
		let year = $(this).data("year");
		let current = $("#year-select .year").text();
		if(year != current)
		{
			setYearGraph($(this).data("year"));
		}
		$("#year-select").removeClass("is-open");  
		$("#year-list").slideUp();
	});
	
	//データダウンロード(Init)
	$("#data-area__filter .dl-button").on("click",function(){
		if($(this).hasClass("init"))
		{
			let file = $(this).data("dl");
			getGraphDate("spot_summary","download",file);
		}
	});
	
	//カレンダー表示
	$("#button--calender-show").on("click",function(){
		$("#datepicker-area").fadeIn();
	});
});	

