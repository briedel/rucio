/* Copyright European Organization for Nuclear Research (CERN)
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * You may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Authors:
 * - Ralph Vigne <ralph.vigne@cern.ch>, 2015 
*/

package ruciotools;

import java.io.*;
import java.util.*;
import java.text.*;

import javax.servlet.ServletException;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import java.io.PrintWriter;

import org.apache.hadoop.security.UserGroupInformation;
import org.apache.hadoop.fs.Path;
import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.hdfs.DistributedFileSystem;
import org.apache.hadoop.fs.FileSystem;
import org.apache.hadoop.fs.FSDataInputStream;
import org.apache.hadoop.fs.FSDataOutputStream;


import org.apache.commons.lang.ArrayUtils;

import org.apache.hadoop.security.UserGroupInformation;
import org.apache.hadoop.fs.Path;
import org.apache.hadoop.conf.Configuration;
import org.apache.hadoop.hdfs.DistributedFileSystem;
import org.apache.hadoop.fs.FileSystem;
import org.apache.hadoop.fs.FileStatus;
import org.apache.hadoop.fs.FSDataInputStream;


/**
 * Servlet implementation class ReadFromHdfs
 */
public class HttpMonitoring extends HttpServlet {
	private static final long serialVersionUID = 1L;
	public static DateFormat dateFormat = new SimpleDateFormat("yyyy-MM-dd", Locale.ENGLISH);
       
	/**
	* @see HttpServlet#HttpServlet()
	*/
	public HttpMonitoring() {
		super();
		// TODO Auto-generated constructor stub
	}

	/**
	 * @see HttpServlet#doGet(HttpServletRequest request, HttpServletResponse response)
	 */
	protected void doGet(HttpServletRequest request, HttpServletResponse response) throws ServletException, IOException {
		final PrintWriter out = response.getWriter();
		final String report_tyoe = request.getParameter("report");
		final String fileName = "http_monitoring_"+request.getParameter("report")+".csv";
		String date = request.getParameter("date");
		String filter = null;

		response.setContentType("text/csv");
		if (!request.getParameterMap().containsKey("date")) {
			SimpleDateFormat sdf = new SimpleDateFormat("yyyy-MM-dd");
			date = new SimpleDateFormat("yyyy-MM-dd").format(new Date());
		}

		if (request.getParameterMap().containsKey("account")) {
			filter = ".*?\t"+request.getParameter("account")+"\t.*$";
		}

		int requestTopN = -1;
		try {
			requestTopN = Integer.parseInt(request.getParameter("top"));
		} catch(NumberFormatException e) {;} catch(Exception e) {System.out.println(e);}

		FileSystem fs = FileSystem.get(new Configuration());
		BufferedReader br=new BufferedReader(new InputStreamReader(fs.open(new Path("/user/rucio01/reports/" + date + "/" + fileName))));

		String line=br.readLine();
		int counter = 0;
		while (line != null){
			if ((requestTopN == -1) || (counter < requestTopN)) {
				if ((filter == null) || (line.matches(filter))) {
					out.write(line+"\n");
					counter++;
				}
			}
			// Read next line
			line=br.readLine();
		}

	}

	/**
	 * @see HttpServlet#doPost(HttpServletRequest request, HttpServletResponse response)
	 */
	protected void doPost(HttpServletRequest request, HttpServletResponse response) throws ServletException, IOException {
		// TODO Auto-generated method stub
	}

}
